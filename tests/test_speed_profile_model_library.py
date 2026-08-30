"""A roster entry gets its own instance profile, deriving from a model profile.

design_new.md "SpeedProfile" / "Train Roster", Profiles are matched by model. A
model profile carries the locomotive model it was measured on. A roster entry
that names no profile finds the model profile measured for its model and gets a
fresh *instance* profile deriving from it — so calibrating one item of a model
gives every locomotive of that model the curve, while each entry owns its own
record and calibrating one never corrupts a sibling. Ties go to the most recent
`calibrated_at`, and an unmeasured placeholder never outranks a real measurement.
"""

from speed_profile import SpeedProfile, SpeedProfileRegistry, make_default_speed_profile
from train_roster import RosterEntry, TrainRoster


def _model_profile(profile_id, model, calibrated_at=None):
    return SpeedProfile(
        profile_id=profile_id, name=profile_id, model=model, calibrated_at=calibrated_at
    )


def _registry(*profiles):
    reg = SpeedProfileRegistry()
    for profile in profiles:
        reg.register(profile)
    return reg


def _derived_from(reg, entry):
    """The model profile the entry's linked instance derives from."""
    instance = reg.get(entry.speed_profile_id)
    assert instance is not None
    assert instance.derived_from is not None  # it is an instance, not a model
    return instance.derived_from


def test_a_model_with_no_profile_links_nothing():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    roster.link_profile_by_model(entry, _registry(_model_profile("p1", "Tomix EF64")))
    assert entry.speed_profile_id is None


def test_an_entry_gets_an_instance_deriving_from_its_model_profile():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    reg = _registry(
        _model_profile("other", "Tomix EF64"), _model_profile("ef66", "Kato EF66")
    )
    roster.link_profile_by_model(entry, reg)
    # The entry names a NEW instance, not the shared model profile itself.
    assert entry.speed_profile_id is not None
    assert entry.speed_profile_id != "ef66"
    assert _derived_from(reg, entry) == "ef66"


def test_two_entries_of_one_model_get_distinct_instances():
    # The anti-corruption property: calibrating one entry writes its own
    # instance, so a sibling of the same model is untouched.
    roster = TrainRoster()
    reg = _registry(_model_profile("ef66", "Kato EF66"))
    a = RosterEntry(entry_id="A", model="Kato EF66")
    b = RosterEntry(entry_id="B", model="Kato EF66")
    roster.link_profile_by_model(a, reg)
    roster.link_profile_by_model(b, reg)
    assert a.speed_profile_id != b.speed_profile_id
    assert _derived_from(reg, a) == "ef66"
    assert _derived_from(reg, b) == "ef66"


def test_the_match_is_exact_on_the_trimmed_string():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="  Kato EF66  ")
    reg = _registry(_model_profile("ef66", "Kato EF66"))
    roster.link_profile_by_model(entry, reg)
    assert _derived_from(reg, entry) == "ef66"

    other = RosterEntry(entry_id="R2", model="Kato EF66 PS22")
    roster.link_profile_by_model(other, _registry(_model_profile("ef66", "Kato EF66")))
    assert other.speed_profile_id is None


def test_the_most_recently_calibrated_model_profile_wins_a_tie():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    reg = _registry(
        _model_profile("old", "Kato EF66", calibrated_at="2026-01-01T00:00:00+00:00"),
        _model_profile("new", "Kato EF66", calibrated_at="2026-08-01T00:00:00+00:00"),
    )
    roster.link_profile_by_model(entry, reg)
    assert _derived_from(reg, entry) == "new"


def test_an_unmeasured_model_profile_loses_to_a_measured_one():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    reg = _registry(
        _model_profile("placeholder", "Kato EF66"),
        _model_profile("measured", "Kato EF66", calibrated_at="2026-01-01T00:00:00+00:00"),
    )
    roster.link_profile_by_model(entry, reg)
    assert _derived_from(reg, entry) == "measured"


def test_an_instance_profile_is_never_a_match_candidate():
    # An instance leaves `model` unset, so even one deriving from the right model
    # is not itself a match — a new entry derives from the model profile, never
    # from another entry's instance.
    roster = TrainRoster()
    reg = _registry(_model_profile("ef66", "Kato EF66"))
    first = RosterEntry(entry_id="R1", model="Kato EF66")
    roster.link_profile_by_model(first, reg)  # registers an instance under ef66
    second = RosterEntry(entry_id="R2", model="Kato EF66")
    roster.link_profile_by_model(second, reg)
    assert _derived_from(reg, second) == "ef66"


def test_an_entry_that_already_names_a_profile_is_left_alone():
    # The match is made once, when the model is written; the stored id is the
    # link every later read follows.
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66", speed_profile_id="chosen")
    roster.link_profile_by_model(entry, _registry(_model_profile("ef66", "Kato EF66")))
    assert entry.speed_profile_id == "chosen"


def test_model_and_calibrated_at_survive_a_round_trip():
    profile = _model_profile("p1", "Kato EF66", calibrated_at="2026-08-10T12:00:00+00:00")
    restored = SpeedProfile.from_dict(profile.to_dict())
    assert restored.model == "Kato EF66"
    assert restored.calibrated_at == "2026-08-10T12:00:00+00:00"


def test_a_seeded_model_profile_is_blank_on_both_fields():
    # Nothing has said which model the train is, and the curve is still the
    # seeded linear default.
    profile = make_default_speed_profile("T1", 100.0)
    assert profile.derived_from is None
    assert profile.model == ""
    assert profile.calibrated_at is None
