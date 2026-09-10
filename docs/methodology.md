# APIx Methodology

**Airfare Price Index for India — SIH26056 (MoSPI)**

This document explains how APIx is built, why each choice was made, and where it
falls short. It is written to be read by someone who is not a programmer.

> **The data behind the numbers in this prototype is simulated.** See
> [What is simulated](#8-what-is-simulated-and-what-is-not) at the end. The
> methodology is real; the fares it is currently applied to are not.

---

## 1. What APIx measures

APIx answers one question: **is it getting more or less expensive to fly within
India?**

That sounds simple, but a naive answer — "average all the fares we can see
today" — is wrong, and wrong in a way that is easy to miss. The average fare
moves when prices move, but it *also* moves when:

- more people happen to be searching for long, expensive routes;
- a low-cost carrier adds capacity, so more cheap seats enter the sample;
- observations happen to be taken closer to departure, where fares are higher.

None of those are price changes. An index has to hold the *composition* of what
it measures constant, so that when the number moves, the only thing that can
have caused it is price.

---

## 2. The formula

APIx is a **Laspeyres-style fixed-basket index**:

$$
\text{APIx}_t = 100 \times \sum_i w_i \cdot \frac{P_{it}}{P_{i0}}
$$

| Symbol | Meaning |
|---|---|
| $i$ | one **basket cell**: a (route × airline × booking-window) combination |
| $w_i$ | that cell's fixed weight — how much it counts |
| $P_{it}$ | the mean fare in cell $i$ on day $t$ |
| $P_{i0}$ | the mean fare in cell $i$ during the **base period** |
| $t$ | the *query date* — the day the fare was observed |

Each cell contributes its **price relative** $P_{it}/P_{i0}$ — how much dearer
or cheaper that specific thing is than it was at the start. Those relatives are
combined using weights that **never change**.

This is the same family of index as the CPI. "Laspeyres" means the weights are
fixed at the base period rather than re-estimated each period.

### Why fixed weights matter

Because the weights are fixed, APIx cannot be moved by a change in the *mix* of
what was observed. If IndiGo suddenly published twice as many fares, its
influence on the index would not change at all — its weight is set by market
share, not by how many rows we happened to collect. This is the single most
important property of the index and the reason the answer is defensible.

---

## 3. The basket

Three dimensions, multiplied together: **8 routes × 4 airlines × 6 booking
windows = 192 cells**.

### Routes (`config/routes.yaml`)

The eight highest-density domestic trunk city-pairs:

| Route | Weight | Route | Weight |
|---|---|---|---|
| DEL–BOM | 0.19 | DEL–HYD | 0.11 |
| DEL–BLR | 0.16 | DEL–MAA | 0.10 |
| BOM–BLR | 0.13 | BOM–MAA | 0.10 |
| DEL–CCU | 0.12 | BLR–MAA | 0.09 |

Weights approximate each pair's share of DGCA-reported domestic city-pair
passenger volumes.

### Airlines (`config/airlines.yaml`)

| Carrier | Weight | Type |
|---|---|---|
| IndiGo (6E) | 0.64 | low-cost |
| Air India group (AI) | 0.27 | full-service |
| Akasa Air (QP) | 0.05 | low-cost |
| SpiceJet (SG) | 0.04 | low-cost |

"Air India group" means the merged entity including Vistara and Air India
Express. Weights approximate DGCA-reported domestic passenger market share.

### Booking windows (`config/index.yaml`)

| Window | Weight | Window | Weight |
|---|---|---|---|
| 0–3 days | 0.12 | 15–21 days | 0.20 |
| 4–7 days | 0.18 | 22–30 days | 0.16 |
| 8–14 days | 0.24 | 31–45 days | 0.10 |

**Why stratify by booking window at all?** Because a fare 3 days before
departure and the same fare 40 days out are different products with different
prices — roughly a 2:1 ratio. If the index did not hold lead time constant, a
shift in *when* fares happened to be observed would look exactly like a change
in the price level. Bucketing removes that.

### Cell weight

$$
w_i = (\text{route traffic share}) \times (\text{airline market share}) \times (\text{booking-window weight})
$$

Each of the three sets sums to 1.0, so the 192 cell weights do too. The code
asserts this at load time rather than trusting it — a mis-normalised basket
would silently rescale every number in the index.

---

## 4. Base period

The base period is the **first date on which data is available**, and APIx is
**100** on that date by construction. Every later value is read as "x% above or
below the starting level".

**A known weakness, stated plainly:** with a single base day, that day's
sampling noise is baked into every subsequent value, because every point in the
series is divided by it. Averaging the first several days would be more stable.
The current setting follows the specification (`base_period.days: 1` in
`config/index.yaml`) and the knob is exposed precisely so this can be changed
and the sensitivity examined.

---

## 5. Handling missing observations

Some cells have no fare on some days — sold out, or nothing published.

When that happens the missing cell's weight is **redistributed proportionally
across the cells that do have a price** that day, rather than treated as zero.

This is a deliberate choice. Treating a missing cell as a zero price would drag
the index *down* whenever seats sold out — the exact opposite of what happens
to prices in reality when availability tightens. Redistribution keeps the index
measuring price rather than availability.

Every day also carries a **coverage** figure: the share of basket weight
actually observed. Days below 60% coverage are flagged rather than published
silently.

Cells with no base-period price are dropped from the basket entirely — without
$P_{i0}$ there is no relative to compute, and substituting a later period's
price would bias the whole series.

---

## 6. Route sub-indices

The same formula restricted to one city-pair. The route weight is constant
within a route so it cancels out, leaving airline × booking-window weights,
renormalised to sum to 1.0. Each route's index is 100 at its own base period,
so routes are directly comparable as *rates of change* — not as price levels.

---

## 7. Lead-time elasticity

**The question:** how much more does a ticket cost for each day you delay
booking?

**The method:** fit a straight line to the logarithm of the fare against
advance-purchase days, per route, then report $e^{-\text{slope}} - 1$ as a
percentage.

**Why the logarithm?** Because fare increases compound. A fare rising 4% per day
is a constant *proportional* rate; fitting rupees against days would fit a
constant *absolute* rate, which under-reads short lead times and over-reads long
ones.

**Why travel-date fixed effects?** This is the subtle part, and getting it wrong
gives a materially wrong number.

In a rolling panel, lead time is entangled with *which departure* is being
priced. A quote taken 45 days out is, by construction, a quote for a departure
six weeks away — and some of those departures are festival dates carrying a
large premium. Fitting naively therefore credits some of the *festival* premium
to *long lead times*, flattening the curve.

Measured on this dataset:

| Estimator | Elasticity | R² |
|---|---|---|
| Pooled OLS (naive) | ~1.11 %/day | 0.28 |
| Travel-date fixed effects | **~1.57 %/day** | **0.51** |

The naive figure understates the effect by roughly 30%. APIx therefore demeans
both the log fare and the lead time within each travel date before fitting, so
the slope is identified **only** by comparing quotes for the *same* departure at
*different* lead times — which is exactly what "what does waiting cost me?"
means.

Current results sit in a tight band of **1.53–1.59 %/day** across the eight
routes, with R² ≈ 0.51.

---

## 8. What is simulated and what is not

**Simulated.** Every fare currently in the index is generated by
`scraper/simulator.py` from the model in `config/simulator.yaml`. No real
airline pricing data is in the database. Rows are tagged `simulated` in a
database column that is constrained to only permit `simulated`, `live`, or
`fallback_simulated`, so provenance cannot be lost or faked downstream.

**Real.** The index mathematics, the weighting scheme, the cleaning pipeline,
the elasticity estimator and the collection machinery are all real and would
operate identically on genuine fares. The live scraper
(`scraper/live_scraper.py`) genuinely attempts a real collection against
Skyscanner and genuinely respects `robots.txt`; it currently receives a
bot-detection challenge, logs it, and falls back to clearly-labelled simulated
data rather than pretending.

**Where real data plugs in.** Nothing in this document changes when real fares
arrive. `fare_quotes` gains rows with `source = 'live'`; every stage downstream
is already source-agnostic.

---

## 9. Known limitations

Stated openly, because they will be asked about:

1. **The fares are simulated.** The methodology is testable; the current numbers
   are not measurements of the Indian air travel market.
2. **Single-day base period.** Noise in one day propagates to the whole series
   (§4).
3. **Booking-window weights are assumptions.** Lead-time booking distributions
   for Indian domestic travel are not published at this granularity. They are
   shaped to a widely-reported pattern, not measured.
4. **Economy cabin only.** Business and premium fares are excluded; the basket
   is fixed at `ECONOMY`.
5. **One-way fares only.** Return and multi-city itineraries price differently.
6. **Eight routes, four carriers.** Broad coverage of trunk traffic, but not the
   full domestic network — regional carriers and thin routes are absent.
7. **A fixed basket ages.** That is the known trade-off of a Laspeyres index: it
   is immune to composition shifts, but it slowly stops resembling the market.
   Real deployment needs a documented rebasing cadence.
8. **Quoted fares, not transacted fares.** APIx measures advertised prices, not
   what passengers actually paid after discounts and corporate rates.

---

## 10. Reproducing the numbers

```bash
python -m scraper.simulator backfill   # generate the fare panel
python -m etl.clean                    # clean it into fares_clean
python -m index.apix --show            # build the index and print it
```

Every threshold and weight referenced above lives in `/config`:
`routes.yaml`, `airlines.yaml`, `simulator.yaml`, `etl.yaml`, `index.yaml`.
