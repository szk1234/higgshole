from tests.live.gating import LIVE_TESTS_ENV, live_tests_enabled


def test_the_gate_is_named_as_the_specification_documents_it():
    # Spec section 8 lists HIGGSHOLE_LIVE_TESTS as the opt-in flag.
    assert LIVE_TESTS_ENV == "HIGGSHOLE_LIVE_TESTS"


def test_billable_tests_are_off_unless_explicitly_enabled():
    assert live_tests_enabled({}) is False
    assert live_tests_enabled({LIVE_TESTS_ENV: ""}) is False
    assert live_tests_enabled({LIVE_TESTS_ENV: "   "}) is False


def test_any_non_empty_value_enables_them():
    assert live_tests_enabled({LIVE_TESTS_ENV: "1"}) is True
    assert live_tests_enabled({LIVE_TESTS_ENV: "yes"}) is True
