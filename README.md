# London fare tracker

Checks Chicago → London round-trip fares every morning at 5:00 AM Central and logs them so you can watch the trend before booking.

**What it searches**
- Chicago (O'Hare or Midway) → London (Heathrow, Gatwick, etc.)
- Out June 15, 16 or 17, 2027; back June 19, 20 or 21 (9 combinations)
- 2 adults, Economy and Premium Economy, nonstop or one stop
- American, United, Aer Lingus, British Airways, Virgin Atlantic
- Drops any outbound that lands after 10:00 AM on June 18, so you have the full day in London

**What you get each day**
- `SUMMARY.md` — today's best fare per cabin, change vs. the last check and the 7-day average, the lowest seen so far, a price grid for every date combination, and a trend chart
- `data/prices.csv` — the cheapest fare per airline for every search (open in Excel or Google Sheets)
- `data/best_fares.csv` — fare type (e.g. Basic vs. Main Cabin) and a booking link for the day's best fare
- `data/runs.csv` — a log of each run, including any errors

## Setup (about 10 minutes)

1. **Create a private repository** on GitHub, e.g. `london-fare-tracker`.
2. **Add these files** (keep the folder structure, including `.github/workflows/track-fares.yml`).
   Easiest: on the repo page choose *Add file → Upload files* and drag in the unzipped folder contents.
   If the hidden `.github` folder doesn't upload, use *Add file → Create new file*, type
   `.github/workflows/track-fares.yml` as the name, and paste the file's contents.
3. **Add your API key as a secret:** *Settings → Secrets and variables → Actions → New repository secret*.
   Name: `IGNAV_API_KEY`, value: your key. (Never put the key in a file.)
4. **Run it once now:** *Actions* tab → *Track Chicago-London fares* → *Run workflow*.
   After a minute or two, open `SUMMARY.md` in the repo.

From then on it runs daily on its own. GitHub emails you if a run fails.

## Things to check on day one
- **Price basis:** compare one fare in `SUMMARY.md` with the booking link or Google Flights to confirm whether prices are for both travelers combined (expected) or per person.
- **"Not bookable yet":** if every search says this, the data source isn't offering June 2027 yet. It will keep trying daily and start logging once fares appear.

## Cost
Each run makes 18 searches plus 2 fare-type lookups (about 600 requests a month).
Ignav's 1,000 free requests last about 7 weeks; after that it's $2 per 1,000 requests, roughly $1.20 a month.
Set a monthly spending limit in your Ignav dashboard if you like.

## Changing what it tracks
All settings are at the top of `tracker.py`: airlines, cabins, dates, stops, and the arrive-by time.
Remove `"premium_economy"` from `CABINS` to halve the request count.

## Stopping it
*Actions* tab → *Track Chicago-London fares* → *⋯* → *Disable workflow*.
