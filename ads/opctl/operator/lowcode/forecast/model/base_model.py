#!/usr/bin/env python
# -*- coding: utf-8 -*--

# Copyright (c) 2023, 2024 Oracle and/or its affiliates.
# Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl/

import fsspec
import numpy as np
import os
import pandas as pd
import tempfile
import time
import traceback
from abc import ABC, abstractmethod
from typing import Tuple

from ads.common.decorator.runtime_dependency import runtime_dependency
from ads.common.object_storage_details import ObjectStorageDetails
from ads.opctl import logger
from ads.opctl.operator.lowcode.common.utils import (
    human_time_friendly,
    enable_print,
    disable_print,
    write_data,
    merged_category_column_name,
    datetime_to_seconds,
    seconds_to_datetime,
)
from ads.opctl.operator.lowcode.forecast.model.forecast_datasets import TestData
from ads.opctl.operator.lowcode.forecast.utils import (
    default_signer,
    evaluate_train_metrics,
    get_forecast_plots,
    get_auto_select_plot,
    _build_metrics_df,
    _build_metrics_per_horizon,
    load_pkl,
    write_pkl,
    _label_encode_dataframe,
)
from .forecast_datasets import ForecastDatasets
from ..const import (
    SUMMARY_METRICS_HORIZON_LIMIT,
    SupportedMetrics,
    SupportedModels,
    SpeedAccuracyMode,
)
from ..operator_config import ForecastOperatorConfig, ForecastOperatorSpec


class ForecastOperatorBaseModel(ABC):
    """The base class for the forecast operator models."""

    def __init__(self, config: ForecastOperatorConfig, datasets: ForecastDatasets):
        """Instantiates the ForecastOperatorBaseModel instance.

        Properties
        ----------
        config: ForecastOperatorConfig
            The forecast operator configuration.
        """
        self.config: ForecastOperatorConfig = config
        self.spec: ForecastOperatorSpec = config.spec
        self.datasets: ForecastDatasets = datasets

        self.full_data_dict = datasets.get_data_by_series()

        self.test_eval_metrics = None
        self.original_target_column = self.spec.target_column
        self.dt_column_name = self.spec.datetime_column.name

        self.model_parameters = dict()
        self.loaded_models = None

        # these fields are populated in the _build_model() method
        self.models = None

        # "outputs" is a list of outputs generated by the models. These should only be generated when the framework requires the original output for plotting
        self.outputs = None
        self.forecast_output = None
        self.errors_dict = dict()
        self.le = dict()

        self.formatted_global_explanation = None
        self.formatted_local_explanation = None

        self.forecast_col_name = "yhat"
        self.perform_tuning = (self.spec.tuning != None) and (
            self.spec.tuning.n_trials != None
        )

    def generate_report(self):
        """Generates the forecasting report."""
        import warnings
        from sklearn.exceptions import ConvergenceWarning

        with warnings.catch_warnings():
            warnings.simplefilter(action="ignore", category=FutureWarning)
            warnings.simplefilter(action="ignore", category=UserWarning)
            warnings.simplefilter(action="ignore", category=RuntimeWarning)
            warnings.simplefilter(action="ignore", category=ConvergenceWarning)
            import report_creator as rc

            # load models if given
            if self.spec.previous_output_dir is not None:
                self._load_model()

            start_time = time.time()
            result_df = self._build_model()
            elapsed_time = time.time() - start_time
            logger.info("Building the models completed in %s seconds", elapsed_time)

            # Generate metrics
            summary_metrics = None
            test_data = None
            self.eval_metrics = None

            if self.spec.generate_report or self.spec.generate_metrics:
                self.eval_metrics = self.generate_train_metrics()

                if self.spec.test_data:
                    try:
                        (
                            self.test_eval_metrics,
                            summary_metrics,
                            test_data,
                        ) = self._test_evaluate_metrics(
                            elapsed_time=elapsed_time,
                        )
                    except Exception as e:
                        logger.warn("Unable to generate Test Metrics.")
                        logger.debug(f"Full Traceback: {traceback.format_exc()}")
            report_sections = []

            if self.spec.generate_report:
                # build the report
                (
                    model_description,
                    other_sections,
                ) = self._generate_report()

                header_section = rc.Block(
                    rc.Heading("Forecast Report", level=1),
                    rc.Text(
                        f"You selected the {self.spec.model} model.\n{model_description}\nBased on your dataset, you could have also selected any of the models: {SupportedModels.keys()}."
                    ),
                    rc.Group(
                        rc.Metric(
                            heading="Analysis was completed in ",
                            value=human_time_friendly(elapsed_time),
                        ),
                        rc.Metric(
                            heading="Starting time index",
                            value=self.datasets.get_earliest_timestamp().strftime(
                                "%B %d, %Y"
                            ),
                        ),
                        rc.Metric(
                            heading="Ending time index",
                            value=self.datasets.get_latest_timestamp().strftime(
                                "%B %d, %Y"
                            ),
                        ),
                        rc.Metric(
                            heading="Num series",
                            value=len(self.datasets.list_series_ids()),
                        ),
                    ),
                )

                first_5_rows_blocks = [
                    rc.DataTable(
                        df.head(5),
                        label=s_id,
                        index=True,
                    )
                    for s_id, df in self.full_data_dict.items()
                ]

                last_5_rows_blocks = [
                    rc.DataTable(
                        df.tail(5),
                        label=s_id,
                        index=True,
                    )
                    for s_id, df in self.full_data_dict.items()
                ]

                data_summary_blocks = [
                    rc.DataTable(
                        df.describe(),
                        label=s_id,
                        index=True,
                    )
                    for s_id, df in self.full_data_dict.items()
                ]

                series_name = merged_category_column_name(
                    self.spec.target_category_columns
                )
                # series_subtext = rc.Text(f"Indexed by {series_name}")
                first_10_title = rc.Heading("First 5 Rows of Data", level=3)
                last_10_title = rc.Heading("Last 5 Rows of Data", level=3)
                summary_title = rc.Heading("Data Summary Statistics", level=3)

                data_summary_sec = rc.Block(
                    rc.Block(
                        first_10_title,
                        # series_subtext,
                        rc.Select(blocks=first_5_rows_blocks),
                    ),
                    rc.Block(
                        last_10_title,
                        # series_subtext,
                        rc.Select(blocks=last_5_rows_blocks),
                    ),
                    rc.Block(
                        summary_title,
                        # series_subtext,
                        rc.Select(blocks=data_summary_blocks),
                    ),
                    rc.Separator(),
                )

                summary = rc.Block(
                    header_section,
                    data_summary_sec,
                )

                test_metrics_sections = []
                if (
                    self.test_eval_metrics is not None
                    and not self.test_eval_metrics.empty
                ):
                    sec7_text = rc.Heading("Test Data Evaluation Metrics", level=2)
                    sec7 = rc.DataTable(self.test_eval_metrics, index=True)
                    test_metrics_sections = test_metrics_sections + [sec7_text, sec7]

                if summary_metrics is not None and not summary_metrics.empty:
                    sec8_text = rc.Heading("Test Data Summary Metrics", level=2)
                    sec8 = rc.DataTable(summary_metrics, index=True)
                    test_metrics_sections = test_metrics_sections + [sec8_text, sec8]

                train_metrics_sections = []
                if self.eval_metrics is not None and not self.eval_metrics.empty:
                    sec9_text = rc.Heading("Training Data Metrics", level=2)
                    sec9 = rc.DataTable(self.eval_metrics, index=True)
                    train_metrics_sections = [sec9_text, sec9]

                backtest_sections = []
                if self.spec.model == "auto-select":
                    output_dir = self.spec.output_directory.url
                    backtest_report_name = "backtest_stats.csv"
                    backtest_stats = pd.read_csv(f"{output_dir}/{backtest_report_name}")
                    average_dict = backtest_stats.mean().to_dict()
                    del average_dict['backtest']
                    best_model = min(average_dict, key=average_dict.get)
                    backtest_text = rc.Heading("Back Testing Metrics", level=2)
                    summary_text = rc.Text(
                        f"Overall, the average scores for the models are {average_dict}, with {best_model}"
                        f" being identified as the top-performing model during backtesting.")
                    backtest_table = rc.DataTable(backtest_stats, index=True)
                    liner_plot = get_auto_select_plot(backtest_stats)
                    backtest_sections = [backtest_text, backtest_table, summary_text, liner_plot]


                forecast_plots = []
                if len(self.forecast_output.list_series_ids()) > 0:
                    forecast_text = rc.Heading(
                        "Forecasted Data Overlaying Historical", level=2
                    )
                    forecast_sec = get_forecast_plots(
                        self.forecast_output,
                        horizon=self.spec.horizon,
                        test_data=test_data,
                        ci_interval_width=self.spec.confidence_interval_width,
                    )
                    if (
                        series_name is not None
                        and len(self.datasets.list_series_ids()) > 1
                    ):
                        forecast_plots = [
                            forecast_text,
                            forecast_sec,
                        ]  # series_subtext,
                    else:
                        forecast_plots = [forecast_text, forecast_sec]

                yaml_appendix_title = rc.Heading("Reference: YAML File", level=2)
                yaml_appendix = rc.Yaml(self.config.to_dict())
                report_sections = (
                    [summary]
                    + backtest_sections
                    + forecast_plots
                    + other_sections
                    + test_metrics_sections
                    + train_metrics_sections
                    + [yaml_appendix_title, yaml_appendix]
                )

            # save the report and result CSV
            self._save_report(
                report_sections=report_sections,
                result_df=result_df,
                metrics_df=self.eval_metrics,
                test_metrics_df=self.test_eval_metrics,
            )

    def _test_evaluate_metrics(self, elapsed_time=0):
        total_metrics = pd.DataFrame()
        summary_metrics = pd.DataFrame()
        data = TestData(self.spec)

        # Generate y_pred and y_true for each series
        for s_id in self.forecast_output.list_series_ids():
            try:
                y_true = data.get_data_for_series(s_id)[data.target_name].values[
                    -self.spec.horizon :
                ]
            except KeyError as ke:
                logger.warn(
                    f"Error Generating Metrics: Unable to find {s_id} in the test data. Error: {ke.args}"
                )
            y_pred = self.forecast_output.get_forecast(s_id)["forecast_value"].values[
                -self.spec.horizon :
            ]

            drop_na_mask = ~np.isnan(y_true) & ~np.isnan(y_pred)
            if not drop_na_mask.all():  # There is a missing value
                if drop_na_mask.any():  # All values are missing
                    logger.debug(
                        f"No values in the test data for series: {s_id}. This will affect the test metrics."
                    )
                    continue
                logger.debug(
                    f"Missing values in the test data for series: {s_id}. This will affect the test metrics."
                )
                y_true = y_true[drop_na_mask]
                y_pred = y_pred[drop_na_mask]

            metrics_df = _build_metrics_df(
                y_true=y_true,
                y_pred=y_pred,
                series_id=s_id,
            )
            total_metrics = pd.concat([total_metrics, metrics_df], axis=1)

        if total_metrics.empty:
            return total_metrics, summary_metrics, data

        summary_metrics = pd.DataFrame(
            {
                SupportedMetrics.MEAN_SMAPE: np.mean(
                    total_metrics.loc[SupportedMetrics.SMAPE]
                ),
                SupportedMetrics.MEDIAN_SMAPE: np.median(
                    total_metrics.loc[SupportedMetrics.SMAPE]
                ),
                SupportedMetrics.MEAN_MAPE: np.mean(
                    total_metrics.loc[SupportedMetrics.MAPE]
                ),
                SupportedMetrics.MEDIAN_MAPE: np.median(
                    total_metrics.loc[SupportedMetrics.MAPE]
                ),
                SupportedMetrics.MEAN_RMSE: np.mean(
                    total_metrics.loc[SupportedMetrics.RMSE]
                ),
                SupportedMetrics.MEDIAN_RMSE: np.median(
                    total_metrics.loc[SupportedMetrics.RMSE]
                ),
                SupportedMetrics.MEAN_R2: np.mean(
                    total_metrics.loc[SupportedMetrics.R2]
                ),
                SupportedMetrics.MEDIAN_R2: np.median(
                    total_metrics.loc[SupportedMetrics.R2]
                ),
                SupportedMetrics.MEAN_EXPLAINED_VARIANCE: np.mean(
                    total_metrics.loc[SupportedMetrics.EXPLAINED_VARIANCE]
                ),
                SupportedMetrics.MEDIAN_EXPLAINED_VARIANCE: np.median(
                    total_metrics.loc[SupportedMetrics.EXPLAINED_VARIANCE]
                ),
                SupportedMetrics.ELAPSED_TIME: elapsed_time,
            },
            index=["All Targets"],
        )

        """Calculates Mean sMAPE, Median sMAPE, Mean MAPE, Median MAPE, Mean wMAPE, Median wMAPE values for each horizon
        if horizon <= 10."""
        if self.spec.horizon <= SUMMARY_METRICS_HORIZON_LIMIT:
            metrics_per_horizon = _build_metrics_per_horizon(
                test_data=data,
                output=self.forecast_output,
            )
            if not metrics_per_horizon.empty:
                summary_metrics = pd.concat([summary_metrics, metrics_per_horizon])

                new_column_order = [
                    SupportedMetrics.MEAN_SMAPE,
                    SupportedMetrics.MEDIAN_SMAPE,
                    SupportedMetrics.MEAN_MAPE,
                    SupportedMetrics.MEDIAN_MAPE,
                    SupportedMetrics.MEAN_WMAPE,
                    SupportedMetrics.MEDIAN_WMAPE,
                    SupportedMetrics.MEAN_RMSE,
                    SupportedMetrics.MEDIAN_RMSE,
                    SupportedMetrics.MEAN_R2,
                    SupportedMetrics.MEDIAN_R2,
                    SupportedMetrics.MEAN_EXPLAINED_VARIANCE,
                    SupportedMetrics.MEDIAN_EXPLAINED_VARIANCE,
                    SupportedMetrics.ELAPSED_TIME,
                ]
                summary_metrics = summary_metrics[new_column_order]

        return total_metrics, summary_metrics, data

    def _save_report(
        self,
        report_sections: Tuple,
        result_df: pd.DataFrame,
        metrics_df: pd.DataFrame,
        test_metrics_df: pd.DataFrame,
    ):
        """Saves resulting reports to the given folder."""
        import report_creator as rc

        unique_output_dir = self.spec.output_directory.url

        if ObjectStorageDetails.is_oci_path(unique_output_dir):
            storage_options = default_signer()
        else:
            storage_options = dict()

        # report-creator html report
        if self.spec.generate_report:
            with tempfile.TemporaryDirectory() as temp_dir:
                report_local_path = os.path.join(temp_dir, "___report.html")
                disable_print()
                with rc.ReportCreator("My Report") as report:
                    report.save(rc.Block(*report_sections), report_local_path)
                enable_print()

                report_path = os.path.join(unique_output_dir, self.spec.report_filename)
                with open(report_local_path) as f1:
                    with fsspec.open(
                        report_path,
                        "w",
                        **storage_options,
                    ) as f2:
                        f2.write(f1.read())

        # forecast csv report
        write_data(
            data=result_df,
            filename=os.path.join(unique_output_dir, self.spec.forecast_filename),
            format="csv",
            storage_options=storage_options,
        )

        # metrics csv report
        if self.spec.generate_metrics:
            metrics_col_name = (
                self.original_target_column
                if self.datasets.has_artificial_series()
                else "Series 1"
            )
            if metrics_df is not None:
                write_data(
                    data=metrics_df.reset_index().rename(
                        {"index": "metrics", "Series 1": metrics_col_name}, axis=1
                    ),
                    filename=os.path.join(
                        unique_output_dir, self.spec.metrics_filename
                    ),
                    format="csv",
                    storage_options=storage_options,
                    index=False,
                )
            else:
                logger.warn(
                    f"Attempted to generate the {self.spec.metrics_filename} file with the training metrics, however the training metrics could not be properly generated."
                )

            # test_metrics csv report
            if self.spec.test_data is not None:
                if test_metrics_df is not None:
                    write_data(
                        data=test_metrics_df.reset_index().rename(
                            {"index": "metrics", "Series 1": metrics_col_name}, axis=1
                        ),
                        filename=os.path.join(
                            unique_output_dir, self.spec.test_metrics_filename
                        ),
                        format="csv",
                        storage_options=storage_options,
                        index=False,
                    )
                else:
                    logger.warn(
                        f"Attempted to generate the {self.spec.test_metrics_filename} file with the test metrics, however the test metrics could not be properly generated."
                    )
        # explanations csv reports
        if self.spec.generate_explanations:
            try:
                if self.formatted_global_explanation is not None:
                    write_data(
                        data=self.formatted_global_explanation,
                        filename=os.path.join(
                            unique_output_dir, self.spec.global_explanation_filename
                        ),
                        format="csv",
                        storage_options=storage_options,
                        index=True,
                    )
                else:
                    logger.warn(
                        f"Attempted to generate global explanations for the {self.spec.global_explanation_filename} file, but an issue occured in formatting the explanations."
                    )

                if self.formatted_local_explanation is not None:
                    write_data(
                        data=self.formatted_local_explanation,
                        filename=os.path.join(
                            unique_output_dir, self.spec.local_explanation_filename
                        ),
                        format="csv",
                        storage_options=storage_options,
                        index=True,
                    )
                else:
                    logger.warn(
                        f"Attempted to generate local explanations for the {self.spec.local_explanation_filename} file, but an issue occured in formatting the explanations."
                    )
            except AttributeError as e:
                logger.warn(
                    "Unable to generate explanations for this model type or for this dataset."
                )
                logger.debug(f"Got error: {e.args}")

        if self.spec.generate_model_parameters:
            # model params
            write_data(
                data=pd.DataFrame.from_dict(self.model_parameters),
                filename=os.path.join(unique_output_dir, "model_params.json"),
                format="json",
                storage_options=storage_options,
                index=True,
                indent=4,
            )

        # model pickle
        if self.spec.generate_model_pickle:
            self._save_model(unique_output_dir, storage_options)

        logger.info(
            f"The outputs have been successfully "
            f"generated and placed into the directory: {unique_output_dir}."
        )
        print(
            f"The outputs have been successfully generated and placed into the directory: {unique_output_dir}."
        )
        if self.errors_dict:
            write_data(
                data=pd.DataFrame.from_dict(self.errors_dict),
                filename=os.path.join(
                    unique_output_dir, self.spec.errors_dict_filename
                ),
                format="json",
                storage_options=storage_options,
                index=True,
                indent=4,
            )
        else:
            logger.info(f"All modeling completed successfully.")

    def preprocess(self, df, series_id):
        """The method that needs to be implemented on the particular model level."""
        data = df.rename(
            {self.dt_column_name: "ds", self.original_target_column: "y"}, axis=1
        )
        self.le[series_id], df_encoded = _label_encode_dataframe(
            data, no_encode={"ds", "y"}
        )
        return df_encoded

    @abstractmethod
    def _generate_report(self):
        """
        Generates the report for the particular model.
        The method that needs to be implemented on the particular model level.
        """

    @abstractmethod
    def _build_model(self) -> pd.DataFrame:
        """
        Build the model.
        The method that needs to be implemented on the particular model level.
        """

    def drop_horizon(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.iloc[: -self.spec.horizon]

    def get_horizon(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.iloc[-self.spec.horizon :]

    def generate_train_metrics(self) -> pd.DataFrame:
        """
        Generate Training Metrics when fitted data is not available.
        The method that needs to be implemented on the particular model level.
        """
        return evaluate_train_metrics(self.forecast_output)

    def _load_model(self):
        try:
            self.loaded_models = load_pkl(self.spec.previous_output_dir + "/model.pkl")
        except:
            logger.info("model.pkl is not present")

    def _save_model(self, output_dir, storage_options):
        write_pkl(
            obj=self.models,
            filename="model.pkl",
            output_dir=output_dir,
            storage_options=storage_options,
        )

    @runtime_dependency(
        module="shap",
        err_msg=(
            "Please run `python3 -m pip install shap` to install the required dependencies for model explanation."
        ),
    )
    def explain_model(self):
        """
        Generates an explanation for the model by using the SHAP (Shapley Additive exPlanations) library.
        This function calculates the SHAP values for each feature in the dataset and stores the results in the `global_explanation` dictionary.

        Returns
        -------
            dict: A dictionary containing the global explanation for each feature in the dataset.
                    The keys are the feature names and the values are the average absolute SHAP values.
        """
        from shap import PermutationExplainer

        datetime_col_name = self.datasets._datetime_column_name

        exp_start_time = time.time()
        global_ex_time = 0
        local_ex_time = 0
        logger.info(
            f"Calculating explanations using {self.spec.explanations_accuracy_mode} mode"
        )
        ratio = SpeedAccuracyMode.ratio[self.spec.explanations_accuracy_mode]

        for s_id, data_i in self.datasets.get_data_by_series(
            include_horizon=False
        ).items():
            if s_id in self.models:
                explain_predict_fn = self.get_explain_predict_fn(series_id=s_id)
                data_trimmed = data_i.tail(
                    max(int(len(data_i) * ratio), 5)
                ).reset_index(drop=True)
                data_trimmed[datetime_col_name] = data_trimmed[datetime_col_name].apply(
                    lambda x: x.timestamp()
                )

                # Explainer fails when boolean columns are passed

                _, data_trimmed_encoded = _label_encode_dataframe(
                    data_trimmed,
                    no_encode={datetime_col_name, self.original_target_column},
                )

                kernel_explnr = PermutationExplainer(
                    model=explain_predict_fn, masker=data_trimmed_encoded
                )
                kernel_explnr_vals = kernel_explnr.shap_values(data_trimmed_encoded)
                exp_end_time = time.time()
                global_ex_time = global_ex_time + exp_end_time - exp_start_time
                self.local_explainer(
                    kernel_explnr, series_id=s_id, datetime_col_name=datetime_col_name
                )
                local_ex_time = local_ex_time + time.time() - exp_end_time

                if not len(kernel_explnr_vals):
                    logger.warn(
                        f"No explanations generated. Ensure that additional data has been provided."
                    )
                else:
                    self.global_explanation[s_id] = dict(
                        zip(
                            data_trimmed.columns[1:],
                            np.average(np.absolute(kernel_explnr_vals[:, 1:]), axis=0),
                        )
                    )
            else:
                logger.warn(
                    f"Skipping explanations for {s_id}, as forecast was not generated."
                )

        logger.info(
            "Global explanations generation completed in %s seconds", global_ex_time
        )
        logger.info(
            "Local explanations generation completed in %s seconds", local_ex_time
        )

    def local_explainer(self, kernel_explainer, series_id, datetime_col_name) -> None:
        """
        Generate local explanations using a kernel explainer.

        Parameters
        ----------
            kernel_explainer: The kernel explainer object to use for generating explanations.
        """
        data = self.datasets.get_horizon_at_series(s_id=series_id)
        # columns that were dropped in train_model in arima, should be dropped here as well
        data[datetime_col_name] = datetime_to_seconds(data[datetime_col_name])
        data = data.reset_index(drop=True)

        # Explainer fails when boolean columns are passed
        _, data = _label_encode_dataframe(
            data, no_encode={datetime_col_name, self.original_target_column}
        )
        # Generate local SHAP values using the kernel explainer
        local_kernel_explnr_vals = kernel_explainer.shap_values(data)

        # Convert the SHAP values into a DataFrame
        local_kernel_explnr_df = pd.DataFrame(
            local_kernel_explnr_vals, columns=data.columns
        )
        self.local_explanation[series_id] = local_kernel_explnr_df

    def get_explain_predict_fn(self, series_id, fcst_col_name="yhat"):
        def _custom_predict(
            data,
            model=self.models[series_id],
            dt_column_name=self.datasets._datetime_column_name,
        ):
            """
            data: ForecastDatasets.get_data_at_series(s_id)
            """
            data[dt_column_name] = seconds_to_datetime(
                data[dt_column_name], dt_format=self.spec.datetime_column.format
            )
            data = self.preprocess(df=data, series_id=series_id)
            data[self.original_target_column] = None
            fcst = model.predict(data)[fcst_col_name]
            return fcst

        return _custom_predict
