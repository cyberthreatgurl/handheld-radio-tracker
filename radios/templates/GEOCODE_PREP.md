Read [](file:///Users/ashaw/code/radio-tracker/radios/signals.py#1-1), lines 1 to 120

Searched for text `address` (`**/radios/templates/radios/manufacturer_form.html`), 11 results

Searched for text `address widget` (`**/radios/forms.py`), no results

Read [](file:///Users/ashaw/code/radio-tracker/radios/templates/radios/manufacturer_form.html#65-65), lines 65 to 115

Searched for regex `address.*Textarea|Textarea.*address|ManufacturerForm`, 5 results

Read [](file:///Users/ashaw/code/radio-tracker/radios/forms.py#371-371), lines 371 to 423

Here's what matters for getting reliable geocodes from Nominatim in this system, organized by the decisions that actually affect outcomes:

---

## How the current geocoder splits addresses

`geocode_address()` splits on commas and builds four fallback queries:

| Attempt | Query built from | Precision stored |
|---------|-----------------|-----------------|
| 1 | All parts | `full` |
| 2 | Parts 1 onwards (street dropped) | `city` |
| 3 | Parts 2 onwards | `state` |
| 4 | Last part only | `country` |

This means the **comma structure is load-bearing** — the order and number of comma-separated segments directly controls what each fallback attempt sends to Nominatim.

---

## Rule 1: Always use specific → general order

Nominatim expects Western/OSM order even for countries (China, Japan, Korea) that traditionally write addresses general → specific.

```
✓  Shenzhen, Guangdong, China
✗  China, Guangdong Province, Shenzhen     ← fallback queries become nonsense
```

The current placeholder already demonstrates this correctly.

---

## Rule 2: Use English romanization, not native script

Nominatim's search API handles native characters inconsistently depending on whether OSM has dual-language tags for that location. English is safe everywhere.

```
✓  Shenzhen, Guangdong, China
✗  深圳市, 广东省, 中国

✓  Osaka, Osaka Prefecture, Japan
✗  大阪市, 大阪府, 日本
```

---

## Rule 3: Skip hyper-specific parts that OSM won't have

OSM coverage of Chinese industrial addresses is patchy below the city level. Sub-district names, industrial park names, building numbers, and floor/room numbers almost never resolve. Start at the street or district level, and if geocoding returns `state` or `country` precision, simplify:

```
✗  Building 7, Longhua Science and Technology Park, Longhua District, Shenzhen, Guangdong, China
✓  Longhua District, Shenzhen, Guangdong, China
✓  Shenzhen, Guangdong, China              (acceptable for map pinning)
```

---

## Rule 4: Country must always be the last comma-separated term

The `country`-level fallback uses `parts[-1]` — whatever is after the last comma. If the postal code comes last, the country fallback will query the postal code alone and fail.

```
✓  Shenzhen, Guangdong, China 518000       (postal code attached to country, usually fine)
✓  Shenzhen, Guangdong, China              (cleanest)
✗  518000, Shenzhen, Guangdong, China      (postal code first confuses the full query)
```

---

## Recommended formats by region

Most manufacturers in this database are Chinese, with a few Japanese, Korean, and Taiwanese. These formats work reliably with Nominatim:

| Region | Recommended format |
|--------|-------------------|
| **China** | `City, Province, China` or `District, City, Province, China` |
| **Japan** | `City, Prefecture, Japan` e.g. `Osaka, Osaka Prefecture, Japan` |
| **Taiwan** | `City, Taiwan` e.g. `Hsinchu, Taiwan` |
| **South Korea** | `City, Province, South Korea` e.g. `Incheon, South Korea` |
| **USA** | `City, State, USA` (standard works perfectly) |
| **Europe** | `City, Country` is usually sufficient |

---

## What to do when geocoding returns coarse precision

The form already shows color-coded feedback. When it shows amber (state) or orange (country), the address is probably too specific for OSM to resolve but too vague to help. The fix is usually:

1. Remove sub-district or industrial park names
2. Check the city name spelling (e.g. "Quanzhou" not "Quan Zhou")
3. Try with just city + province + country (3 comma parts)
4. As a last resort, use coordinates directly — the `latitude`/`longitude` fields could be set via the admin panel if Nominatim keeps failing for a known location

Given how the geocoder splits on commas and constructs fallback queries, here's what to input:

```
Huli District, Xiamen, Fujian, China
```

**Why, part by part:**

| Issue | Problem | Fix |
|---|---|---|
| Company name (`Xiamen Radtel Electronics Co.,Ltd`) | Goes in the **Name/Full Name** field, not address | Omit entirely from the address field |
| `502, No.1 Jinyi, Wuli` | The comma inside the street number fragments the geocoder — `502` becomes `parts[0]`, making the `city` fallback start with `No.1 Jinyi` which Nominatim won't resolve | Omit the street/building number; Nominatim doesn't have enough OSM data at that level for Chinese industrial addresses anyway |
| `Wuli` | Sub-neighbourhood; not in OSM | Omit |
| `Huli` → use `Huli District` | Huli is a district of Xiamen; adding "District" helps Nominatim disambiguate | Keep as first part |
| `Xiamen City` → `Xiamen` | "City" suffix is redundant and can confuse Nominatim | Drop "City" |
| `P.R CHINA` | Nominatim may not recognise this variant | Use `China` — it must be the **last** comma part since the country-level fallback is `parts[-1]` |
| Postal code `361004` | If appended after `China` (e.g. `China 361004`), the country fallback query becomes `China 361004` — generally fine, but unnecessary noise | Omit |

**What each fallback attempt will send to Nominatim:**

| Precision | Query |
|---|---|
| `full` | `Huli District, Xiamen, Fujian, China` |
| `city` | `Xiamen, Fujian, China` |
| `state` | `Fujian, China` |
| `country` | `China` |

All four are clean and progressively resolvable. Xiamen has solid OSM coverage so you should get at least `city`-level precision, likely `full`.