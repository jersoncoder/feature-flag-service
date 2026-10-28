from __future__ import annotations

import pytest

from helpers import chi_square_sf


# Upper 5% and 0.1% critical values from standard chi-square tables.
@pytest.mark.parametrize(
    ("statistic", "dof", "p_value"),
    [
        (3.841, 1, 0.05),
        (5.991, 2, 0.05),
        (7.815, 3, 0.05),
        (18.307, 10, 0.05),
        (123.225, 99, 0.05),
        (10.828, 1, 0.001),
        (13.816, 2, 0.001),
        (16.266, 3, 0.001),
    ],
)
def test_chi_square_sf_matches_tables(statistic: float, dof: int, p_value: float) -> None:
    assert chi_square_sf(statistic, dof) == pytest.approx(p_value, rel=2e-3)


def test_chi_square_sf_edges() -> None:
    assert chi_square_sf(0.0, 3) == 1.0
    with pytest.raises(ValueError, match="dof"):
        chi_square_sf(1.0, 0)
