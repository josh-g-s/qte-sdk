import re

import qte_sdk


def test_package_imports_with_a_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", qte_sdk.__version__)


def test_ci_must_fail():
    import os
    assert False
