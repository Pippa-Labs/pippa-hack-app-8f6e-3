# Test fixtures

These are real public responses from the Food Standards Agency ratings API
(api.ratings.food.gov.uk, sent with `x-api-version: 2`) and postcodes.io. The tests
use them so they can run offline. No records were trimmed or edited, with two exceptions:

- Every `LocalAuthorityEmailAddress` value (council food-safety inboxes) was replaced with
  `redacted@example.com`, because this repo refuses files that contain email addresses.
- Each file was re-saved with `json.dumps(obj, indent=1, ensure_ascii=False)`. A few long
  floats such as `Distance` may now show fewer digits, but they parse to the same value.

Every request sent the User-Agent `CleanPlate-PippaAgentApp/0.2 (food hygiene rating lookup; https://github.com/Pippa-Labs)`.

| File | URL fetched | When |
|---|---|---|
| fsa_dishoom_name_only.json | https://api.ratings.food.gov.uk/Establishments?name=dishoom&pageSize=20 | fetched 2026-11-20 |
| fsa_dishoom_address_kings_cross.json | https://api.ratings.food.gov.uk/Establishments?name=dishoom&address=King%27s%20Cross&pageSize=20 | fetched 2026-11-20 |
| postcodes_places_kings_cross.json | https://api.postcodes.io/places?q=King%27s%20Cross&limit=10 | fetched 2026-11-20 |
| fsa_dishoom_geo_kings_cross.json | https://api.ratings.food.gov.uk/Establishments?name=dishoom&latitude=51.5369449512001&longitude=-0.11914370871063379&maxDistanceLimit=1&sortOptionKey=distance&pageSize=20 | fetched 2026-11-20 |
| fsa_bao_address_soho.json | https://api.ratings.food.gov.uk/Establishments?name=Bao&address=Soho&pageSize=20 | fetched 2026-11-20 |
| postcodes_places_soho.json | https://api.postcodes.io/places?q=Soho&limit=10 | fetched 2026-11-20 |
| fsa_bao_geo_soho.json | https://api.ratings.food.gov.uk/Establishments?name=Bao&latitude=51.51297873202493&longitude=-0.13370712010377248&maxDistanceLimit=1&sortOptionKey=distance&pageSize=20 | fetched 2026-11-20 |
| fsa_franco_manca_address_brixton.json | https://api.ratings.food.gov.uk/Establishments?name=Franco%20Manca&address=Brixton&pageSize=20 | fetched 2026-11-20 |
| postcodes_places_brixton.json | https://api.postcodes.io/places?q=Brixton&limit=10 | fetched 2026-11-20 |
| fsa_franco_manca_geo_brixton.json | https://api.ratings.food.gov.uk/Establishments?name=Franco%20Manca&latitude=51.46103625406251&longitude=-0.11115278863927161&maxDistanceLimit=1&sortOptionKey=distance&pageSize=20 | fetched 2026-11-20 |
| postcodes_postcode_sw72az.json | https://api.postcodes.io/postcodes/SW72AZ | fetched 2026-11-20 |
| fsa_pret_geo_sw72az.json | https://api.ratings.food.gov.uk/Establishments?name=Pret&latitude=51.499334&longitude=-0.179178&maxDistanceLimit=1&sortOptionKey=distance&pageSize=20 | fetched 2026-11-20 |
| postcodes_outcode_sw7.json | https://api.postcodes.io/outcodes/SW7 | fetched 2026-11-20 |
| postcodes_postcode_not_found.json | https://api.postcodes.io/postcodes/ZZ11ZZ (HTTP 404 body) | fetched 2026-11-20 |
| fsa_chicken_address_exhibition_road.json | https://api.ratings.food.gov.uk/Establishments?name=chicken&address=Exhibition%20Road&pageSize=20 | fetched 2026-11-20 |
| postcodes_places_exhibition_road.json | https://api.postcodes.io/places?q=Exhibition%20Road&limit=10 | fetched 2026-11-20 |
| fsa_chicken_name_only.json | https://api.ratings.food.gov.uk/Establishments?name=chicken&pageSize=50 | fetched 2026-11-20 |
| postcodes_postcode_eh11bb.json | https://api.postcodes.io/postcodes/EH11BB | fetched 2026-11-20 |
| fsa_pret_geo_eh11bb.json | https://api.ratings.food.gov.uk/Establishments?name=Pret&latitude=55.951957&longitude=-3.189945&maxDistanceLimit=1&sortOptionKey=distance&pageSize=20 | fetched 2026-11-20 |
| fsa_improvement_required.json | https://api.ratings.food.gov.uk/Establishments?name=10%20to%2010%20in%20Delhi&pageSize=5 ("10 to 10 in Delhi", EH8 9BZ, Edinburgh, "Improvement Required") | fetched 2026-11-20 |
| fsa_awaiting_inspection.json | https://api.ratings.food.gov.uk/Establishments?name=Parea%20hospitality&pageSize=5 ("Parea hospitality ltd", SE22 9EP, "AwaitingInspection") | fetched 2026-11-20 |
| fsa_exempt.json | https://api.ratings.food.gov.uk/Establishments?name=Science%20Museum%20Shop&pageSize=5 ("Science Museum Shop", SW7 2DD, "Exempt") | fetched 2026-11-20 |
| fsa_no_results.json | https://api.ratings.food.gov.uk/Establishments?name=zzqxnotarealplace&pageSize=20 | fetched 2026-11-20 |

The geo coordinates are exactly what postcodes.io returned: for `/places`, the result whose
`name_1` matches the place, preferring the Greater London one (Soho and Brixton both have a
non-London namesake that is skipped); for postcodes, `result.latitude`/`result.longitude`.
The improvement-required listing was found with the FSA filter `ratingKey=ImprovementRequired`.
