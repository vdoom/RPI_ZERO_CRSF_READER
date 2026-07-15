from jetson_bridge.channel_scaler import crsf_to_us, scale_channels


def test_anchor_points():
    assert crsf_to_us(172) == 988
    assert crsf_to_us(992) == 1500
    assert crsf_to_us(1811) == 2012


def test_scale_channels_clamps_extremes():
    # 0 -> 880 us (below range), 2047 -> 2159 us (above range)
    assert scale_channels([0] * 16) == [988] * 16
    assert scale_channels([2047] * 16) == [2012] * 16


def test_scale_channels_passes_normal_range():
    channels = [172, 992, 1811] + [992] * 13
    assert scale_channels(channels) == [988, 1500, 2012] + [1500] * 13


def test_custom_clamp():
    assert scale_channels([0] * 16, us_min=1000, us_max=2000) == [1000] * 16
    assert scale_channels([2047] * 16, us_min=1000, us_max=2000) == [2000] * 16


def test_monotonic_over_full_range():
    previous = None
    for value in range(0, 2048):
        us = crsf_to_us(value)
        if previous is not None:
            assert us >= previous
        previous = us
