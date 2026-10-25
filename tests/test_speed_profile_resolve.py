"""`SpeedProfileRegistry.resolve` — the model/instance overlay.

design_new.md "SpeedProfile", Model profiles and instance profiles. A record is
a model profile (`derived_from` unset, a complete record) or an instance profile
(`derived_from` set, carrying only its own measured `lighting_voltage`,
`power_speed_curve`, and `calibrated_at`). `resolve` returns a fully-filled
profile: the instance's fields where it carries them, the model's everywhere
else, and a `voltage_power_curve` rebuilt from the model's curve re-anchored at
the resolved `lighting_voltage`. A model profile resolves to itself unchanged.
"""

from speed_profile import (
    SpeedProfile,
    SpeedProfileRegistry,
    make_instance_profile,
    reanchor_voltage_power_curve,
    seed_default_profile_pair,
)


def _model():
    return SpeedProfile(
        profile_id="M",
        name="Kato EF66",
        model="Kato EF66",
        lighting_voltage=1.5,
        max_voltage=12.0,
        power_speed_curve=[(0.0, 0.0), (100.0, 90.0)],
        acceleration_rate=7.5,
    )


# ---- a model profile resolves to itself -----------------------------------


def test_a_model_profile_resolves_to_itself_unchanged():
    reg = SpeedProfileRegistry()
    model = _model()
    reg.register(model)
    assert reg.resolve("M") is model


def test_derived_from_is_none_on_a_model_profile():
    assert _model().derived_from is None


# ---- an instance overlays its model ---------------------------------------


def test_an_empty_instance_reads_every_field_from_its_model():
    reg = SpeedProfileRegistry()
    reg.register(_model())
    inst = make_instance_profile("M", profile_id="I", name="EF66 #1")
    reg.register(inst)

    resolved = reg.resolve("I")
    # The resolved record carries the instance's id (a train that names the
    # instance still matches its resolved profile) ...
    assert resolved.profile_id == "I"
    # ... but every value comes from the model until the instance measures one.
    assert resolved.lighting_voltage == 1.5
    assert resolved.max_voltage == 12.0
    assert resolved.acceleration_rate == 7.5
    assert resolved.power_speed_curve == [(0.0, 0.0), (100.0, 90.0)]
    assert resolved.calibrated_at is None
    assert resolved.power_percent_to_speed(100.0) == 90.0


def test_an_instance_field_that_is_set_wins_over_the_model():
    reg = SpeedProfileRegistry()
    reg.register(_model())
    inst = make_instance_profile("M", profile_id="I")
    inst.power_speed_curve = [(0.0, 0.0), (100.0, 60.0)]
    inst.calibrated_at = "2026-08-10T12:00:00+00:00"
    reg.register(inst)

    resolved = reg.resolve("I")
    # The instance's measured curve and stamp win ...
    assert resolved.power_speed_curve == [(0.0, 0.0), (100.0, 60.0)]
    assert resolved.calibrated_at == "2026-08-10T12:00:00+00:00"
    # ... while the fields it does not carry still come from the model.
    assert resolved.acceleration_rate == 7.5
    assert resolved.lighting_voltage == 1.5


def test_calibrated_at_follows_the_curve():
    """design_new.md "SpeedProfile", `calibrated_at` follows the curve: an
    instance running on its own curve reads its own stamp, so a curve drawn by
    hand never carries the model profile's measurement date, while an instance
    running on the model's curve reads the model's stamp."""
    reg = SpeedProfileRegistry()
    model = _model()
    model.calibrated_at = "2026-08-10T12:00:00+00:00"
    reg.register(model)
    drawn = make_instance_profile("M", profile_id="DRAWN")
    drawn.power_speed_curve = [(0.0, 0.0), (100.0, 70.0)]
    reg.register(drawn)
    linked = make_instance_profile("M", profile_id="LINKED")
    reg.register(linked)

    assert reg.resolve("DRAWN").power_speed_curve == [(0.0, 0.0), (100.0, 70.0)]
    assert reg.resolve("DRAWN").calibrated_at is None
    assert reg.resolve("LINKED").calibrated_at == "2026-08-10T12:00:00+00:00"


# ---- voltage_power_curve reconstruction -----------------------------------


def test_an_instance_stores_no_voltage_power_curve():
    inst = make_instance_profile("M", profile_id="I")
    inst.lighting_voltage = 3.0
    # Even if a caller tried to set one, __post_init__ keeps it empty.
    forced = SpeedProfile(
        profile_id="I2",
        name="",
        derived_from="M",
        voltage_power_curve=[(1.0, 0.0), (12.0, 100.0)],
    )
    assert inst.voltage_power_curve == []
    assert forced.voltage_power_curve == []


def test_resolve_reanchors_the_voltage_power_curve_at_the_instance_lighting():
    reg = SpeedProfileRegistry()
    reg.register(_model())  # model curve default: [(1.5, 0%), (12.0, 100%)]
    inst = make_instance_profile("M", profile_id="I")
    inst.lighting_voltage = 3.0
    reg.register(inst)

    resolved = reg.resolve("I")
    # Bottom anchor moved to the instance's lighting voltage; the model's 1.5 V
    # 0%-point is dropped (it names a voltage that does not move this loco).
    assert resolved.voltage_power_curve == [(3.0, 0.0), (12.0, 100.0)]
    # At exactly the lighting voltage, power is 0.
    assert resolved.voltage_to_power(3.0) == 0.0


def test_reanchor_helper_drops_points_below_the_new_anchor():
    curve = [(1.5, 0.0), (4.0, 30.0), (12.0, 100.0)]
    # Anchor above 4.0 drops both the old anchor and the (4.0, 30%) point.
    assert reanchor_voltage_power_curve(curve, 5.0, 12.0) == [(5.0, 0.0), (12.0, 100.0)]
    # Anchor equal to the existing bottom is a no-op in shape.
    assert reanchor_voltage_power_curve(curve, 1.5, 12.0) == [
        (1.5, 0.0),
        (4.0, 30.0),
        (12.0, 100.0),
    ]


# ---- pair seeding ----------------------------------------------------------


def test_seed_default_profile_pair_registers_a_model_and_an_instance():
    reg = SpeedProfileRegistry()
    instance_id = seed_default_profile_pair(reg, "T60", 60.0)

    instance = reg.get(instance_id)
    assert instance.derived_from is not None
    # The instance carries no measured fields of its own yet.
    assert instance.power_speed_curve is None
    assert instance.lighting_voltage is None
    # The model it derives from is registered and anchored to the train's speed.
    model = reg.get(instance.derived_from)
    assert model.derived_from is None
    assert model.power_percent_to_speed(100.0) == 60.0
    # Resolving the instance yields the model's max-speed-anchored curve.
    assert reg.resolve(instance_id).power_percent_to_speed(100.0) == 60.0


def test_a_missing_model_still_resolves_to_a_drivable_profile():
    reg = SpeedProfileRegistry()
    inst = make_instance_profile("GONE", profile_id="I")
    reg.register(inst)  # derived_from names a model that is not registered

    resolved = reg.resolve("I")
    assert resolved is not None
    # Falls back to the model-profile defaults so a consumer is never handed a
    # partial record.
    assert resolved.lighting_voltage == 1.5
    assert resolved.power_speed_curve == [(0.0, 0.0), (100.0, 100.0)]
    assert resolved.voltage_power_curve  # synthesized, non-empty


# ---- round trip preserves the kind ----------------------------------------


def test_instance_survives_a_dict_round_trip_unset_fields_stay_unset():
    inst = make_instance_profile("M", profile_id="I", name="EF66 #1")
    restored = SpeedProfile.from_dict(inst.to_dict())
    assert restored.derived_from == "M"
    assert restored.power_speed_curve is None
    assert restored.lighting_voltage is None
    assert restored.calibrated_at is None
    assert restored.voltage_power_curve == []


def test_a_measured_instance_survives_a_dict_round_trip():
    inst = make_instance_profile("M", profile_id="I")
    inst.power_speed_curve = [(0.0, 0.0), (100.0, 55.0)]
    inst.lighting_voltage = 2.25
    inst.calibrated_at = "2026-08-10T12:00:00+00:00"
    restored = SpeedProfile.from_dict(inst.to_dict())
    assert restored.derived_from == "M"
    assert restored.power_speed_curve == [(0.0, 0.0), (100.0, 55.0)]
    assert restored.lighting_voltage == 2.25
    assert restored.calibrated_at == "2026-08-10T12:00:00+00:00"
