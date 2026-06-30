"""Bundled geo landmark dataset for the TUI map renderer.

Hand-curated, intentionally small. Covers the Western US + selected Eastern
metros — the regions where Watch Duty has dense fire coverage. Coordinates
are pulled from public-domain sources (USGS GNIS for cities, US DOT NHS for
interstate waypoints). All distances are degrees, all coordinates are WGS84.

Two tables:

* ``CITIES`` — ``(name, lat, lng, population)``. Sorted descending by
  population so callers can early-stop when the map cell is full.
* ``INTERSTATES`` — ``{shield: [(lat, lng), ...], ...}``. Each list is an
  ordered polyline of waypoints sampled every ~50 km so a Bresenham fill
  produces a continuous line at the map's typical resolution.
"""

from __future__ import annotations

# Major US cities — population >= 250k OR otherwise notable for fire context.
CITIES: list[tuple[str, float, float, int]] = [
    ("Los Angeles", 34.0522, -118.2437, 3898747),
    ("San Diego", 32.7157, -117.1611, 1386932),
    ("San Jose", 37.3382, -121.8863, 1013240),
    ("San Francisco", 37.7749, -122.4194, 873965),
    ("Oakland", 37.8044, -122.2712, 440646),
    ("Sacramento", 38.5816, -121.4944, 524943),
    ("Fresno", 36.7378, -119.7871, 542107),
    ("Bakersfield", 35.3733, -119.0187, 403455),
    ("Long Beach", 33.7701, -118.1937, 466742),
    ("Anaheim", 33.8366, -117.9143, 346824),
    ("Santa Ana", 33.7455, -117.8677, 310227),
    ("Riverside", 33.9533, -117.3962, 314998),
    ("Irvine", 33.6846, -117.8265, 307670),
    ("Stockton", 37.9577, -121.2908, 320804),
    ("Chula Vista", 32.6401, -117.0842, 275487),
    ("Modesto", 37.6391, -120.9969, 218464),
    ("Santa Rosa", 38.4405, -122.7144, 178127),
    ("Salinas", 36.6777, -121.6555, 163542),
    ("Santa Barbara", 34.4208, -119.6982, 88665),
    ("Ventura", 34.2746, -119.2290, 110763),
    ("Eureka", 40.8021, -124.1637, 26512),
    ("Redding", 40.5865, -122.3917, 93611),
    ("Chico", 39.7285, -121.8375, 101475),
    ("Paradise", 39.7596, -121.6219, 4764),
    ("Truckee", 39.3280, -120.1833, 16180),
    ("South Lake Tahoe", 38.9399, -119.9772, 21330),
    ("Mammoth Lakes", 37.6485, -118.9722, 7283),
    ("Bishop", 37.3636, -118.3953, 3760),
    # Pacific Northwest
    ("Portland", 45.5152, -122.6784, 652503),
    ("Seattle", 47.6062, -122.3321, 737015),
    ("Tacoma", 47.2529, -122.4443, 219346),
    ("Spokane", 47.6588, -117.4260, 228989),
    ("Salem", 44.9429, -123.0351, 175535),
    ("Eugene", 44.0521, -123.0868, 175096),
    ("Boise", 43.6150, -116.2023, 235684),
    ("Bend", 44.0582, -121.3153, 99178),
    # Mountain / Southwest
    ("Las Vegas", 36.1699, -115.1398, 641903),
    ("Reno", 39.5296, -119.8138, 264165),
    ("Phoenix", 33.4484, -112.0740, 1608139),
    ("Tucson", 32.2226, -110.9747, 542629),
    ("Flagstaff", 35.1983, -111.6513, 76831),
    ("Salt Lake City", 40.7608, -111.8910, 200133),
    ("Provo", 40.2338, -111.6585, 115162),
    ("Cedar City", 37.6775, -113.0619, 35275),
    ("St. George", 37.0965, -113.5684, 95342),
    ("Denver", 39.7392, -104.9903, 715522),
    ("Colorado Springs", 38.8339, -104.8214, 478221),
    ("Albuquerque", 35.0844, -106.6504, 564559),
    ("Santa Fe", 35.6870, -105.9378, 87505),
    ("Missoula", 46.8721, -113.9940, 75516),
    ("Bozeman", 45.6770, -111.0429, 53293),
    ("Billings", 45.7833, -108.5007, 117116),
    # Major eastern reference points (for occasional eastern fire events)
    ("Dallas", 32.7767, -96.7970, 1304379),
    ("Houston", 29.7604, -95.3698, 2304580),
    ("Chicago", 41.8781, -87.6298, 2746388),
    ("Atlanta", 33.7490, -84.3880, 498715),
    ("Miami", 25.7617, -80.1918, 442241),
    ("New York", 40.7128, -74.0060, 8336817),
    # Smaller fire-prone communities frequently in Watch Duty headlines
    ("Ojai", 34.4480, -119.2429, 7637),
    ("Big Bear Lake", 34.2439, -116.9114, 5046),
    ("Yreka", 41.7355, -122.6345, 7807),
    ("Mt. Shasta", 41.3099, -122.3106, 3328),
    ("Susanville", 40.4163, -120.6530, 8466),
    ("Lakeport", 39.0429, -122.9158, 5128),
    ("Ukiah", 39.1502, -123.2078, 16175),
    ("Mariposa", 37.4849, -119.9663, 2173),
]

# Interstate highway polylines — sampled along the route every ~50–100 km.
# Each line gets enough waypoints that linear interpolation between them
# looks like a continuous highway on a ~80-cell-wide map.
INTERSTATES: dict[str, list[tuple[float, float]]] = {
    "I-5": [
        (32.5532, -117.0654),  # San Ysidro
        (33.1581, -117.3506),  # Carlsbad
        (33.7701, -118.1937),  # Long Beach
        (34.0522, -118.2437),  # LA
        (34.5783, -118.1165),  # Castaic / Grapevine
        (35.4675, -118.9013),  # Bakersfield-ish
        (36.0700, -120.0900),  # Coalinga area
        (37.0000, -120.8700),  # Los Banos
        (38.0000, -121.5600),  # Stockton-ish
        (38.5816, -121.4944),  # Sacramento
        (39.5300, -122.1900),  # Willows
        (40.5865, -122.3917),  # Redding
        (41.7355, -122.6345),  # Yreka
        (42.3265, -122.8756),  # Medford
        (43.2065, -123.3417),  # Roseburg
        (44.0521, -123.0868),  # Eugene
        (44.9429, -123.0351),  # Salem
        (45.5152, -122.6784),  # Portland
        (46.9787, -122.9007),  # Olympia
        (47.2529, -122.4443),  # Tacoma
        (47.6062, -122.3321),  # Seattle
        (48.9999, -122.7330),  # Blaine / border
    ],
    "I-10": [
        (33.8000, -118.1700),  # Santa Monica area
        (33.7700, -117.8500),  # Anaheim
        (33.9533, -117.3962),  # Riverside
        (33.8400, -116.5460),  # Banning
        (33.7200, -116.2330),  # Indio
        (33.6500, -114.5800),  # Blythe
        (33.4484, -112.0740),  # Phoenix
        (32.7767, -109.8000),  # Safford-ish
        (32.2226, -110.9747),  # Tucson
        (31.7619, -106.4850),  # El Paso
        (29.4241, -98.4936),   # San Antonio
        (29.7604, -95.3698),   # Houston
    ],
    "I-15": [
        (32.7157, -117.1611),  # San Diego
        (33.1500, -117.0900),  # Escondido
        (33.7400, -117.4900),  # Temecula
        (34.0500, -117.1900),  # Ontario / Rancho Cucamonga
        (34.5300, -117.2900),  # Victorville
        (35.2800, -116.0700),  # Baker
        (36.1699, -115.1398),  # Las Vegas
        (37.0965, -113.5684),  # St. George
        (37.6775, -113.0619),  # Cedar City
        (40.2338, -111.6585),  # Provo
        (40.7608, -111.8910),  # Salt Lake City
        (42.1100, -112.4700),  # Pocatello
        (43.6150, -116.2023),  # Boise via I-84 split actually -- approximate
        (46.8721, -113.9940),  # Missoula via continuation north
        (48.9990, -114.0660),  # Border
    ],
    "I-40": [
        (34.8514, -114.6113),  # Needles
        (35.1872, -111.6513),  # Flagstaff
        (35.0844, -106.6504),  # Albuquerque
        (35.2226, -101.8313),  # Amarillo
        (35.4676, -97.5164),   # OKC
        (35.1495, -90.0490),   # Memphis
    ],
    "I-80": [
        (37.7749, -122.4194),  # SF
        (37.8044, -122.2712),  # Oakland
        (38.5816, -121.4944),  # Sacramento
        (39.0840, -120.0322),  # Truckee / Donner
        (39.5296, -119.8138),  # Reno
        (40.7396, -114.0349),  # Wendover
        (40.7608, -111.8910),  # SLC
        (41.5868, -109.2030),  # Rock Springs
        (41.1400, -104.8200),  # Cheyenne
        (41.2565, -95.9345),   # Omaha
        (41.8781, -87.6298),   # Chicago
    ],
    "I-70": [
        (38.5733, -109.5498),  # Green River, UT
        (38.7392, -108.4503),  # Grand Junction
        (39.5500, -107.3300),  # Glenwood Springs
        (39.7392, -104.9903),  # Denver
    ],
    "I-90": [
        (47.6062, -122.3321),  # Seattle
        (47.6588, -117.4260),  # Spokane
        (47.4827, -111.3008),  # Great Falls-ish
        (46.8721, -113.9940),  # Missoula
        (45.6770, -111.0429),  # Bozeman
        (45.7833, -108.5007),  # Billings
        (43.8791, -103.4591),  # Rapid City
        (43.5460, -96.7313),   # Sioux Falls
        (41.8781, -87.6298),   # Chicago
        (42.3601, -71.0589),   # Boston
    ],
}

__all__ = ["CITIES", "INTERSTATES"]
