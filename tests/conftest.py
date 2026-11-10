import pandas as pd
import pytest

from mqresolver import synth
from mqresolver.ingest import load_inventory
from mqresolver.model import build_model


def make_df(**kw) -> pd.DataFrame:
    cols, rows = synth.generate(**kw)
    return pd.DataFrame(rows, columns=cols).astype(str)


@pytest.fixture(scope="session")
def sample_df():
    return make_df()


@pytest.fixture(scope="session")
def small_df():
    return make_df(apps=60, qms=25, flows=110, seed=7)


@pytest.fixture(scope="session")
def sample_inv(sample_df):
    return load_inventory(sample_df)


@pytest.fixture(scope="session")
def sample_model(sample_inv):
    return build_model(sample_inv.df, sample_inv.attribute_columns)


@pytest.fixture(scope="session")
def small_inv(small_df):
    return load_inventory(small_df)


@pytest.fixture(scope="session")
def small_model(small_inv):
    return build_model(small_inv.df, small_inv.attribute_columns)
