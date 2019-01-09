# -*- coding: utf-8 -*-
from __future__ import print_function
import warnings
from datetime import datetime
import time

import numpy as np
import pandas as pd
from numpy.linalg import LinAlgError
from scipy.integrate import trapz

from lifelines.fitters import BaseFitter
from lifelines.utils import (
    _get_index,
    inv_normal_cdf,
    epanechnikov_kernel,
    ridge_regression as lr,
    qth_survival_times,
    pass_for_numeric_dtypes_or_raise,
    concordance_index,
    check_nans_or_infs,
    ConvergenceWarning,
    check_low_var,
    normalize,
    string_justify,
    _to_list,
    format_floats,
    significance_codes_as_text,
    format_p_value,
)

from lifelines.utils.progress_bar import progress_bar
from lifelines.plotting import fill_between_steps


class AalenAdditiveFitter(BaseFitter):

    r"""
    This class fits the regression model:

    .. math::  h(t|x)  = b_0(t) + b_1(t) x_1 + ... + b_N(t) x_N

    that is, the hazard rate is a linear function of the covariates with time-varying coefficients. 
    This implementation assumes non-time-varying covariates, see ``TODO: name``

    Note
    -----

    This class was rewritten in lifelines 0.17.0 to focus solely on static datasets. 
    There is no guarantee of backwards compatibility.

    Parameters
    -----------
    fit_intercept: bool, optional (default: True)
      If False, do not attach an intercept (column of ones) to the covariate matrix. The
      intercept, :math:`b_0(t)` acts as a baseline hazard.
    alpha: float
      the level in the confidence intervals.
    coef_penalizer: float, optional (default: 0)
      Attach a L2 penalizer to the size of the coeffcients during regression. This improves
      stability of the estimates and controls for high correlation between covariates.
      For example, this shrinks the absolute value of :math:`c_{i,t}`.
    smoothing_penalizer: float, optional (default: 0)
      Attach a L2 penalizer to difference between adjacent (over time) coefficents. For
      example, this shrinks the absolute value of :math:`c_{i,t} - c_{i,t+1}`.

    """

    def __init__(self, fit_intercept=True, alpha=0.95, coef_penalizer=0.0, smoothing_penalizer=0.0):
        self.fit_intercept = fit_intercept
        self.alpha = alpha
        self.coef_penalizer = coef_penalizer
        self.smoothing_penalizer = smoothing_penalizer

        if not (0 < alpha <= 1.0):
            raise ValueError("alpha parameter must be between 0 and 1.")
        if coef_penalizer < 0 or smoothing_penalizer < 0:
            raise ValueError("penalizer parameters must be >= 0.")

    def fit(self, df, duration_col, event_col=None, weights_col=None, show_progress=False):
        """
        Parameters
        ----------
        Fit the Aalen Additive model to a dataset.

        Parameters
        ----------
        df: DataFrame
            a Pandas dataframe with necessary columns `duration_col` and
            `event_col` (see below), covariates columns, and special columns (weights).
            `duration_col` refers to
            the lifetimes of the subjects. `event_col` refers to whether
            the 'death' events was observed: 1 if observed, 0 else (censored).

        duration_col: string
            the name of the column in dataframe that contains the subjects'
            lifetimes.

        event_col: string, optional
            the  name of thecolumn in dataframe that contains the subjects' death
            observation. If left as None, assume all individuals are uncensored.

        weights_col: string, optional
            TODO

        show_progress: boolean, optional (default=False)
            Since the fitter is iterative, show iteration number.


        Returns
        -------
        self: AalenAdditiveFitter
            self with additional new properties: ``cumulative_hazards_``, etc.

        Examples
        --------
        >>> from lifelines import AalenAdditiveFitter
        >>>
        >>> df = pd.DataFrame({
        >>>     'T': [5, 3, 9, 8, 7, 4, 4, 3, 2, 5, 6, 7],
        >>>     'E': [1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 0],
        >>>     'var': [0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2],
        >>>     'age': [4, 3, 9, 8, 7, 4, 4, 3, 2, 5, 6, 7],
        >>> })
        >>>
        >>> aaf = AalenAdditiveFitter()
        >>> aaf.fit(df, 'T', 'E')
        >>> aaf.predict_median(df)

        """
        self._time_fit_was_called = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC"

        df = df.copy()

        self.duration_col = duration_col
        self.event_col = event_col
        self.weights_col = weights_col

        self._n_examples = df.shape[0]

        X, T, E, weights = self._preprocess_dataframe(df)

        self.durations = T.copy()
        self.event_observed = E.copy()
        self.weights = weights.copy()

        self._norm_std = X.std(0)

        # if we included an intercept, we need to fix not divide by zero.
        if self.fit_intercept:
            self._norm_std["baseline"] = 1.0
        else:
            # a baseline was provided
            self._norm_std[self._norm_std < 1e-8] = 1.0

        self.cumulative_hazards_, self.cumulative_variance_ = self._fit_model(
            normalize(X, 0, self._norm_std), T, E, weights, show_progress
        )
        self.cumulative_hazards_ /= self._norm_std
        self.cumulative_variance_ /= self._norm_std
        self.confidence_intervals_ = self._compute_confidence_intervals()

        self._predicted_hazards_ = self.predict_cumulative_hazard(X).iloc[-1].values.ravel()
        return self

    def _fit_model(self, X, T, E, weights, show_progress):

        columns = X.columns
        index = np.sort(np.unique(T[E]))

        hazards_, variance_hazards_ = self._fit_model_to_data_batch(
            X.values, T.values, E.values, weights.values, show_progress
        )

        cumulative_hazards_ = pd.DataFrame(hazards_, columns=columns, index=index).cumsum()
        cumulative_variance_hazards_ = pd.DataFrame(variance_hazards_, columns=columns, index=index).cumsum()

        return cumulative_hazards_, cumulative_variance_hazards_

    def _fit_model_to_data_batch(self, X, T, E, weights, show_progress):

        n, d = X.shape

        # we are mutating values of X, so copy it.
        X = X.copy()

        # iterate over all the unique death times
        unique_death_times = np.sort(np.unique(T[E]))
        n_deaths = unique_death_times.shape[0]
        total_observed_exits = 0

        hazards_ = np.zeros((n_deaths, d))
        variance_hazards_ = np.zeros((n_deaths, d))
        v = np.zeros(d)
        start = time.time()

        for i, t in enumerate(unique_death_times):

            exits = T == t
            deaths = exits & E
            try:
                R = lr(X, deaths, c1=self.coef_penalizer, c2=self.smoothing_penalizer, offset=v)
                V = R[:, :-1]
                v = R[:, -1]
            except LinAlgError:
                warnings.warn(
                    "Linear regression error at index=%d, time=%.3f. Try increasing the coef_penalizer value." % (i, t),
                    ConvergenceWarning,
                )
                v = np.zeros(d)
                # TODO: handle V here.

            hazards_[i, :] = v

            variance_hazards_[i, :] = (V[:, deaths] ** 2).sum(1)

            X[exits, :] = 0

            if show_progress:
                print("Iteration %d/%d, seconds_since_start = %.2f" % (i + 1, n_deaths, time.time() - start))

            # terminate early when there are less than (3 * d) subjects left, where d does not include the intercept.
            # the value 3 if from R survival lib.
            if (3 * (d - 1)) >= n - total_observed_exits:
                if show_progress:
                    print("Terminating early due to too few subjects in the tail. This is expected behaviour.")
                break

            total_observed_exits += exits.sum()

        return hazards_, variance_hazards_

    def _preprocess_dataframe(self, df):
        n, d = df.shape

        df = df.sort_values(by=self.duration_col)

        # Extract time and event
        T = df.pop(self.duration_col)
        E = df.pop(self.event_col) if (self.event_col is not None) else pd.Series(np.ones(n), index=df.index, name="E")
        W = (
            df.pop(self.weights_col)
            if (self.weights_col is not None)
            else pd.Series(np.ones((n,)), index=df.index, name="weights")
        )

        # check to make sure their weights are okay
        if self.weights_col:
            if (W.astype(int) != W).any() and not self.robust:
                warnings.warn(
                    """It appears your weights are not integers, possibly propensity or sampling scores then?
It's important to know that the naive variance estimates of the coefficients are biased."
""",
                    StatisticalWarning,
                )
            if (W <= 0).any():
                raise ValueError("values in weight column %s must be positive." % self.weights_col)

        self._check_values(df, T, E)

        if self.fit_intercept:
            assert (
                "baseline" not in df.columns
            ), "baseline is an internal lifelines column, please rename your column first."
            df["baseline"] = 1.0

        X = df.astype(float)
        T = T.astype(float)
        E = E.astype(bool)

        return X, T, E, W

    def predict_cumulative_hazard(self, X):
        """
        Returns the hazard rates for the individuals

        Parameters
        ----------
        X: a (n,d) covariate numpy array or DataFrame. If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.

        """
        n, _ = X.shape

        cols = _get_index(X)
        if isinstance(X, pd.DataFrame):
            order = self.cumulative_hazards_.columns
            order = order.drop("baseline") if self.fit_intercept else order
            X_ = X[order].values
        else:
            X_ = X

        X_ = X_ if not self.fit_intercept else np.c_[X_, np.ones((n, 1))]

        timeline = self.cumulative_hazards_.index
        individual_cumulative_hazards_ = pd.DataFrame(
            np.dot(self.cumulative_hazards_, X_.T), index=timeline, columns=cols
        )

        return individual_cumulative_hazards_

    def _check_values(self, X, T, E):
        pass_for_numeric_dtypes_or_raise(X)
        check_nans_or_infs(T)
        check_nans_or_infs(E)
        check_nans_or_infs(X)

    def predict_survival_function(self, X):
        """
        Returns the survival functions for the individuals

        Parameters
        ----------
        X: a (n,d) covariate numpy array or DataFrame
            If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.

        """
        return np.exp(-self.predict_cumulative_hazard(X))

    def predict_percentile(self, X, p=0.5):
        """
        Returns the median lifetimes for the individuals.
        http://stats.stackexchange.com/questions/102986/percentile-loss-functions

        Parameters
        ----------
        X: a (n,d) covariate numpy array or DataFrame
            If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.

        """
        index = _get_index(X)
        return qth_survival_times(p, self.predict_survival_function(X)[index]).T

    def predict_median(self, X):
        """
        
        Parameters
        ----------
        X: a (n,d) covariate numpy array or DataFrame
            If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.

        Returns the median lifetimes for the individuals
        """
        return self.predict_percentile(X, 0.5)

    def predict_expectation(self, X):
        """
        Compute the expected lifetime, E[T], using covariates X.
        
        Parameters
        ----------
        X: a (n,d) covariate numpy array or DataFrame
            If a DataFrame, columns
            can be in any order. If a numpy array, columns must be in the
            same order as the training data.

        Returns the expected lifetimes for the individuals
        """
        index = _get_index(X)
        t = self.cumulative_hazards_.index
        return pd.DataFrame(trapz(self.predict_survival_function(X)[index].values.T, t), index=index)

    def _compute_confidence_intervals(self):
        alpha2 = inv_normal_cdf(1 - (1 - self.alpha) / 2)
        std_error = np.sqrt(self.cumulative_variance_)
        return pd.concat(
            {
                "lower-bound": self.cumulative_hazards_ - alpha2 * std_error,
                "upper-bound": self.cumulative_hazards_ + alpha2 * std_error,
            }
        )

    def plot(self, columns=None, loc=None, iloc=None, **kwargs):
        """"
        A wrapper around plotting. Matplotlib plot arguments can be passed in, plus:

        Parameters
        -----------
        columns: string or list-like, optional
          If not empty, plot a subset of columns from the ``cumulative_hazards_``. Default all.
        ix: slice, optional
          specify a time-based subsection of the curves to plot, ex:
                 ``.plot(loc=slice(0.,10.))`` will plot the time values between t=0. and t=10.
        iloc: slice, optional
          specify a location-based subsection of the curves to plot, ex:
                 ``.plot(iloc=slice(0,10))`` will plot the first 10 time points.
        """
        from matplotlib import pyplot as plt

        assert loc is None or iloc is None, "Cannot set both loc and iloc in call to .plot"

        def shaded_plot(ax, x, y, y_upper, y_lower, **kwargs):
            base_line, = ax.plot(x, y, drawstyle="steps-post", **kwargs)
            fill_between_steps(x, y_lower, y2=y_upper, ax=ax, alpha=0.25, color=base_line.get_color(), linewidth=1.0)

        def create_df_slicer(loc, iloc):
            get_method = "loc" if loc is not None else "iloc"

            if iloc is None and loc is None:
                user_submitted_ix = slice(0, None)
            else:
                user_submitted_ix = loc if loc is not None else iloc

            return lambda df: getattr(df, get_method)[user_submitted_ix]

        subset_df = create_df_slicer(loc, iloc)

        if not columns:
            columns = self.cumulative_hazards_.columns
        else:
            columns = _to_list(columns)

        ax = kwargs.get("ax", None) or plt.figure().add_subplot(111)

        x = subset_df(self.cumulative_hazards_).index.values.astype(float)

        for column in columns:
            y = subset_df(self.cumulative_hazards_[column]).values
            y_upper = subset_df(self.confidence_intervals_[column].loc["upper-bound"]).values
            y_lower = subset_df(self.confidence_intervals_[column].loc["lower-bound"]).values
            shaded_plot(ax, x, y, y_upper, y_lower, label=column)

        ax.legend()
        return ax

    def smoothed_hazards_(self, bandwidth=1):
        """
        Using the epanechnikov kernel to smooth the hazard function, with sigma/bandwidth

        """
        timeline = self.cumulative_hazards_.index
        return pd.DataFrame(
            np.dot(epanechnikov_kernel(timeline[:, None], timeline, bandwidth), self.hazards_.values),
            columns=self.hazards_.columns,
            index=timeline,
        )

    @property
    def score_(self):
        """
        The concordance score (also known as the c-index) of the fit.  The c-index is a generalization of the AUC
        to survival data, including censorships.

        For this purpose, the ``score_`` is a measure of the predictive accuracy of the fitted model
        onto the training dataset. It's analgous to the R^2 in linear models.

        """
        # pylint: disable=access-member-before-definition
        if hasattr(self, "_predicted_hazards_"):
            self._concordance_score_ = concordance_index(self.durations, -self._predicted_hazards_, self.event_observed)
            del self._predicted_hazards_
            return self._concordance_score_
        return self._concordance_score_

    @property
    def summary(self):
        """Summary statistics describing the fit.
        Set alpha property in the object before calling.

        Returns
        -------
        df : DataFrame
            Contains columns coef, exp(coef), se(coef), z, p, lower, upper"""

        diff = lambda s: s - s.shift().fillna(0)
        variance_weights_sum = (1 / self.cumulative_variance_).sum()

        df = pd.DataFrame(index=self.cumulative_hazards_.columns)
        df["avg(coef)"] = (self.cumulative_hazards_ / self.cumulative_variance_).sum() / variance_weights_sum
        df["avg(lower %.2f)" % self.alpha] = (
            self.confidence_intervals_.loc["lower-bound"] / self.cumulative_variance_
        ).sum() / variance_weights_sum
        df["avg(upper %.2f)" % self.alpha] = (
            self.confidence_intervals_.loc["upper-bound"] / self.cumulative_variance_
        ).sum() / variance_weights_sum
        return df

    def print_summary(self, decimals=2, **kwargs):
        """
        Print summary statistics describing the fit, the coefficients, and the error bounds.

        Parameters
        -----------
        decimals: int, optional (default=2)
            specify the number of decimal places to show
        kwargs:
            print additional metadata in the output (useful to provide model names, dataset names, etc.) when comparing 
            multiple outputs. 

        """

        # Print information about data first
        justify = string_justify(18)
        print(self)
        print("{} = '{}'".format(justify("duration col"), self.duration_col))
        print("{} = '{}'".format(justify("event col"), self.event_col))
        if self.weights_col:
            print("{} = '{}'".format(justify("weights col"), self.weights_col))

        print("{} = {}".format(justify("number of subjects"), self._n_examples))
        print("{} = {}".format(justify("number of events"), self.event_observed.sum()))
        print("{} = {}".format(justify("time fit was run"), self._time_fit_was_called))

        for k, v in kwargs.items():
            print("{} = {}\n".format(justify(k), v))

        print(end="\n")
        print("---")

        df = self.summary
        # Significance codes as last column
        # df[""] = [significance_code(p) for p in df["p"]]
        print(df.to_string(float_format=format_floats(decimals), formatters={"p": format_p_value(decimals)}))

        # Significance code explanation
        print("---")
        print(significance_codes_as_text(), end="\n\n")
        print("Concordance = {:.{prec}f}".format(self.score_, prec=decimals))
