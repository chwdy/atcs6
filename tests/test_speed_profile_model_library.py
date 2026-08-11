"""A `SpeedProfile` is a model library entry, not a per-locomotive record.

design_new.md "SpeedProfile" / "Train Roster", Profiles are matched by model. A
profile carries the locomotive model it was measured on, and a roster entry that
names no profile links the one already measured for its model — so calibrating
one item of a model gives every locomotive of that model the curve. Ties go to
the most recent `calibrated_at`, and an unmeasured placeholder never outranks a
real measurement.
"""

from speed_profile import SpeedProfile
from train_roster import RosterEntry, TrainRoster


def _profile(profile_id, model, calibrated_at=None):
    return SpeedProfile(
        profile_id=profile_id, name=profile_id, model=model, calibrated_at=calibrated_at
    )


def test_a_model_with_no_profile_links_nothing():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    roster.link_profile_by_model(entry, [_profile("p1", "Tomix EF64")])
    assert entry.speed_profile_id is None


def test_an_entry_links_the_profile_measured_for_its_model():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    profiles = [_profile("other", "Tomix EF64"), _profile("ef66", "Kato EF66")]
    roster.link_profile_by_model(entry, profiles)
    assert entry.speed_profile_id == "ef66"


def test_the_match_is_exact_on_the_trimmed_string():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="  Kato EF66  ")
    roster.link_profile_by_model(entry, [_profile("ef66", "Kato EF66")])
    assert entry.speed_profile_id == "ef66"

    other = RosterEntry(entry_id="R2", model="Kato EF66 PS22")
    roster.link_profile_by_model(other, [_profile("ef66", "Kato EF66")])
    assert other.speed_profile_id is None


def test_the_most_recently_calibrated_profile_wins_a_tie():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    profiles = [
        _profile("old", "Kato EF66", calibrated_at="2026-01-01T00:00:00+00:00"),
        _profile("new", "Kato EF66", calibrated_at="2026-08-01T00:00:00+00:00"),
    ]
    roster.link_profile_by_model(entry, profiles)
    assert entry.speed_profile_id == "new"


def test_an_unmeasured_profile_loses_to_a_measured_one():
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66")
    profiles = [
        _profile("placeholder", "Kato EF66"),
        _profile("measured", "Kato EF66", calibrated_at="2026-01-01T00:00:00+00:00"),
    ]
    roster.link_profile_by_model(entry, profiles)
    assert entry.speed_profile_id == "measured"


def test_an_entry_that_already_names_a_profile_is_left_alone():
    # The match is made once, when the model is written; the stored id is the
    # link every later read follows.
    roster = TrainRoster()
    entry = RosterEntry(entry_id="R1", model="Kato EF66", speed_profile_id="chosen")
    roster.link_profile_by_model(entry, [_profile("ef66", "Kato EF66")])
    assert entry.speed_profile_id == "chosen"


def test_model_and_calibrated_at_survive_a_round_trip():
    profile = _profile("p1", "Kato EF66", calibrated_at="2026-08-10T12:00:00+00:00")
    restored = SpeedProfile.from_dict(profile.to_dict())
    assert restored.model == "Kato EF66"
    assert restored.calibrated_at == "2026-08-10T12:00:00+00:00"


def test_a_seeded_profile_is_blank_on_both_fields():
    # Nothing has said which model the train is, and the curve is still the
    # seeded linear default.
    from speed_profile import make_default_speed_profile

    profile = make_default_speed_profile("T1", 100.0)
    assert profile.model == ""
    assert profile.calibrated_at is None
