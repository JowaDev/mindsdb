import numpy as np
import pandas as pd
from pandas.tseries.frequencies import to_offset
from sklearn.metrics import r2_score

# handle optional dependency
try:
    import hierarchicalforecast   # noqa: F401
    from hierarchicalforecast.core import HierarchicalReconciliation
    from hierarchicalforecast.methods import BottomUp
    from hierarchicalforecast.utils import aggregate
except (ImportError, SystemError):
    HierarchicalReconciliation = None
    BottomUp = None
    aggregate = None

from mindsdb.utilities import log

DEFAULT_FREQUENCY = "D"
DEFAULT_RECONCILER = BottomUp


def transform_to_nixtla_df(df, settings_dict, exog_vars=[]):
    """Transform dataframes into the specific format required by StatsForecast.

    Nixtla packages require dataframes to have the following columns:
        unique_id -> the grouping column. If multiple groups are specified then
        we join them into one name using a / char.
        ds -> the date series
        y -> the target variable for prediction

    You can optionally include exogenous regressors after these three columns, but
    they must be numeric.
    """
    nixtla_df = df.copy()

    # Resample every group
    freq = settings_dict['frequency']
    resampled_df = pd.DataFrame(columns=nixtla_df.columns)
    if settings_dict["group_by"] and settings_dict["group_by"] != ['__group_by']:
        for group, groupdf in nixtla_df.groupby(by=settings_dict["group_by"]):
            groupdf.index = pd.to_datetime(groupdf.pop(settings_dict["order_by"]))
            resampled_groupdf = pd.DataFrame(groupdf[settings_dict['target']].resample(freq).mean())
            for k, v in zip(settings_dict["group_by"], group):
                resampled_groupdf[k] = v
            resampled_groupdf = resampled_groupdf.reset_index()
            resampled_df = pd.concat([resampled_df, resampled_groupdf])
        nixtla_df = resampled_df

    # Transform group columns into single unique_id column
    if len(settings_dict["group_by"]) > 1:
        for col in settings_dict["group_by"]:
            nixtla_df[col] = nixtla_df[col].astype(str)
        nixtla_df["unique_id"] = nixtla_df[settings_dict["group_by"]].agg("/".join, axis=1)
        group_col = "ignore this"
    else:
        group_col = settings_dict["group_by"][0]

    # Rename columns to statsforecast names
    nixtla_df = nixtla_df.rename(
        {settings_dict["target"]: "y", settings_dict["order_by"]: "ds", group_col: "unique_id"}, axis=1
    )

    if "unique_id" not in nixtla_df.columns:
        # add to dataframe as it is expected by statsforecast
        nixtla_df["unique_id"] = '1'

    columns_to_keep = ["unique_id", "ds", "y"] + exog_vars
    nixtla_df["ds"] = pd.to_datetime(nixtla_df["ds"])
    return nixtla_df[columns_to_keep]


def get_results_from_nixtla_df(nixtla_df, model_args):
    """Transform dataframes generated by StatsForecast back to their original format.

    This will return the dataframe to the original format supplied by the MindsDB query.
    """
    return_df = nixtla_df.reset_index(drop=True if 'unique_id' in nixtla_df.columns else False)
    if len(model_args["group_by"]) > 0:
        if len(model_args["group_by"]) > 1:
            for i, group in enumerate(model_args["group_by"]):
                return_df[group] = return_df["unique_id"].apply(lambda x: x.split("/")[i])
        else:
            group_by_col = model_args["group_by"][0]
            return_df[group_by_col] = return_df["unique_id"]

    return return_df.drop(["unique_id"], axis=1).rename({"ds": model_args["order_by"]}, axis=1)


def infer_frequency(df, time_column, default=DEFAULT_FREQUENCY):
    try:  # infer frequency from time column
        date_series = pd.to_datetime(df.sort_values(by=time_column)[time_column]).unique()
        inferred_freq = pd.infer_freq(date_series)  # call this first to get e.g. months & other irregular periods right
        if inferred_freq is None:
            values, counts = np.unique(np.diff(date_series), return_counts=True)
            delta = values[np.argmax(counts)]
            inferred_freq = to_offset(pd.to_timedelta(delta)).freqstr
    except TypeError:
        inferred_freq = default
    return inferred_freq if inferred_freq is not None else default


def get_model_accuracy_dict(nixtla_results_df, metric=r2_score):
    """Calculates accuracy for each model in the nixtla results df."""
    accuracy_dict = {}
    for column in nixtla_results_df.columns:
        if column in ["unique_id", "ds", "y", "cutoff"]:
            continue
        model_error = metric(nixtla_results_df["y"], nixtla_results_df[column])
        accuracy_dict[column] = model_error
    return accuracy_dict


def get_best_model_from_results_df(nixtla_results_df, metric=r2_score):
    """Gets the best model based, on lowest error, from a results df
    with a column for each nixtla model.
    """
    best_model, current_accuracy = None, 0
    accuracy_dict = get_model_accuracy_dict(nixtla_results_df, metric)
    for model, accuracy in accuracy_dict.items():
        if accuracy > current_accuracy:
            best_model, current_accuracy = model, accuracy
    return best_model


def spec_hierarchy_from_list(col_list):
    """Gets the hierarchy spec from the list of hierarchy cols"""
    spec = [["Total"]]
    for i in range(len(col_list)):
        spec.append(["Total"] + col_list[: i + 1])
    return spec


def get_hierarchy_from_df(df, model_args):
    """Extracts hierarchy from the raw df, using the provided spec and args.

    The "hierarchy" model arg is a list of format
    [<level 1>, <level 2>, ..., <level n>]
    where each element is a level in the hierarchy.

    We return a tuple (nixtla_df, hier_df, hier_dict) where:
    nixtla_df is a dataframe in the format nixtla packages uses for training
    hier_df is a matrix of 0s and 1s showing the hierarchical structure
    hier_dict is a dictionary with the hierarchical structure. See the unit test
    in tests/unit/ml_handlers/test_time_series_utils.py for an example.
    """
    if HierarchicalReconciliation is not None:
        spec = spec_hierarchy_from_list(model_args["hierarchy"])

        nixtla_df = df.rename({model_args["order_by"]: "ds", model_args["target"]: "y"}, axis=1)
        nixtla_df["ds"] = pd.to_datetime(nixtla_df["ds"])
        for col in model_args["group_by"]:
            nixtla_df[col] = nixtla_df[col].astype(str)  # grouping columns need to be string format
        nixtla_df.insert(0, "Total", "total")

        nixtla_df, hier_df, hier_dict = aggregate(nixtla_df, spec)  # returns (nixtla_df, hierarchy_df, hierarchy_dict)
        return nixtla_df, hier_df, hier_dict
    else:
        log.logger.warning("HierarchicalForecast is not installed, but `get_hierarchy_from_df` has been called. This should never happen.")  # noqa


def reconcile_forecasts(nixtla_df, forecast_df, hierarchy_df, hierarchy_dict):
    """Reconciles forecast results according to the hierarchy."""
    if HierarchicalReconciliation is not None:
        reconcilers = [DEFAULT_RECONCILER()]
        hrec = HierarchicalReconciliation(reconcilers=reconcilers)
        reconciled_df = hrec.reconcile(Y_hat_df=forecast_df, Y_df=nixtla_df, S=hierarchy_df, tags=hierarchy_dict)
        return get_results_from_reconciled_df(reconciled_df, hierarchy_df)
    else:
        log.logger.warning("HierarchicalForecast is not installed, but `reconcile_forecasts` has been called. This should never happen.")  # noqa


def get_results_from_reconciled_df(reconciled_df, hierarchy_df):
    """Formats the reconciled df into a normal Nixtla results df.

    First drops the model output columns that haven't been reconciled.
    Then drops rows corresponding to higher level predictions that were not
    in the original dataframe, e.g. the total for each grouping.
    """
    #  Drop unnecessary columns
    for col in reconciled_df.columns:
        if col not in ["ds", "y"]:
            if "BottomUp" not in col:
                results_df = reconciled_df.drop(col, axis=1)  # removes original forecast column
                break

    #  Drop higher-level rows
    lowest_level_ids = hierarchy_df.columns
    results_df = results_df[results_df.index.isin(lowest_level_ids)]
    results_df.index = results_df.index.str.replace("total/", "")
    return results_df
