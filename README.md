# Road Trip for Home Assistant

Bring your fuel log from the [Road Trip](https://darrensoft.ca/roadtrip/) iOS app into Home Assistant.

Road Trip has no cloud API, so this integration takes the app's own backup file (a `.zip` of
`.roadtrip` vehicle files), decodes it locally, and turns it into sensors and chart data.
An iPhone Shortcut can send new backups automatically.

## What you get

For every vehicle in the app, a device with these sensors:

| Sensor | Example |
|---|---|
| Fuel economy (last 5 tanks) | 11.4 L/100 km |
| Fuel economy (lifetime) | 11.8 L/100 km |
| Fuel price (last fill-up) | 32.64 ZAR/L |
| Fuel spend (12 months) / (all time) | 38 473 ZAR |
| Distance (12 months) | 13 238 km |
| Fuel cost per km (12 months) | 2.91 ZAR/km |
| Odometer (last fill-up) | 149 889 km |
| Last fill-up | timestamp, with the last 10 fill-ups as an attribute |
| Fill-ups logged | 286 |

Plus:

* a **Road Trip › Last import** sensor with fleet totals
* a `roadtrip_imported` event after every import, for notifications or automations
* `GET /api/roadtrip/data` (authenticated): the full dataset including chart series
  (per-tank economy, rolling average, price paid, monthly and yearly spend)

Consumption is calculated full-tank to full-tank: litres from partial fills are added to the next full
tank, and obvious outliers (a missed fill-up) are left out of the averages. The 12-month figures
roll forward nightly.

## Install

**HACS:** HACS › ⋮ › Custom repositories › add this repo's URL as an *Integration* › download
**Road Trip** › restart Home Assistant.

**Manual:** copy `custom_components/roadtrip` to `/config/custom_components/roadtrip` and restart.

Then: **Settings › Devices & Services › Add integration › Road Trip**. The confirmation screen
shows your private upload address (`…/api/webhook/<id>`). You can find it again under **Configure**.

## Sending backups

### iPhone Shortcut (Share Sheet)

1. New shortcut › ⓘ › enable **Show in Share Sheet**, accepting **Files**.
2. Add **Get Contents of URL**: URL = your webhook address, Method = **POST**,
   Request Body = **File** › *Shortcut Input*.
3. Optional: **Show Notification** with *Contents of URL*.

In Road Trip: **Backup › Share › (your shortcut)**.

### iPhone Shortcut (automatic, from iCloud Drive)

If Road Trip's backups appear in **Files › iCloud Drive**, use **Get Contents of Folder** ›
**Filter Files** (name ends in `.zip`, latest first, limit 1) › **Get Contents of URL** as above, and
trigger it with an automation such as *App › Road Trip › Is Closed* (add a short wait first).

The webhook accepts a raw file body or a multipart form, as a backup `.zip` or individual
`.roadtrip` files. Uploads replace only the vehicles they contain.

By default the webhook only accepts uploads from your local network. To upload from anywhere through
your external or Nabu Casa URL, enable the option under **Configure**.

### From disk

Put a backup in `/config/roadtrip/inbox/` and run the action **Road Trip: Import file**
(`roadtrip.import_file`), optionally with a `path`.

## Dashboard charts

[apexcharts-card](https://github.com/RomRider/apexcharts-card) can read the API directly:

```yaml
type: custom:apexcharts-card
graph_span: 3000d
header: { show: true, title: L/100 km per tank }
series:
  - entity: sensor.road_trip_last_import
    name: Per tank
    stroke_width: 0
    data_generator: |
      const d = await hass.callApi('GET', 'roadtrip/data?vehicle=my_car');
      return d.series.economy;
  - entity: sensor.road_trip_last_import
    name: Rolling average
    data_generator: |
      const d = await hass.callApi('GET', 'roadtrip/data?vehicle=my_car');
      return d.series.economy_rolling;
apex_config:
  markers: { size: [3, 0] }
```

Each vehicle's `slug` is its name in lower case with underscores (`Mini Cooper S` › `mini_cooper_s`).
The `series` keys are `economy`, `economy_rolling`, `price`, `odometer`, `spend_monthly` and `spend_yearly`.

## Notes and limits

* The Road Trip file format isn't documented. The decoder was worked out from real backups (format 2,
  app version 1400). Fill-ups, partial fills, prices, stations and locations are read; trips, services
  and expenses are not yet.
* Metric units (km, litres) are assumed.
* Uploaded files are stored in `/config/roadtrip/vehicles/` and are included in your HA backups.

## Development

```bash
pip install -r requirements_test.txt
ROADTRIP_TEST_BACKUP=/path/to/Road_Trip_Backup.zip pytest
```

Tests are skipped without a backup, because real backups contain personal data and aren't committed.

Not affiliated with darrensoft or the Road Trip app.
