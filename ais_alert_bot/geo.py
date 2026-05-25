from math import asin, atan2, cos, degrees, radians, sin, sqrt


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    rlat1 = radians(lat1)
    rlat2 = radians(lat2)

    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def initial_bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1 = radians(lat1)
    rlat2 = radians(lat2)
    dlon = radians(lon2 - lon1)

    y = sin(dlon) * cos(rlat2)
    x = cos(rlat1) * sin(rlat2) - sin(rlat1) * cos(rlat2) * cos(dlon)
    return (degrees(atan2(y, x)) + 360) % 360
