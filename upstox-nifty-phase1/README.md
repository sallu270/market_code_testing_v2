# Upstox NIFTY Phase 1 Data Collector

This project performs the data-collection part of Phase 1:

- Connects to Upstox Market Data Feed V3.
- Subscribes to configured NIFTY instruments.
- Stores raw tick data in SQLite.
- Builds one-minute OHLC candles locally.
- Pushes updates to a browser dashboard using Server-Sent Events.
- Does not place real or paper orders.

## Requirements

- Python 3.10 or newer.
- An Upstox trading account.
- An Upstox developer application.
- A valid Upstox access token.
- Valid Upstox instrument keys.
- Internet access while collecting live data.
- A modern browser.

## 1. Create the Upstox developer application

Sign in to the Upstox developer portal and create an application.

Configure a redirect URL that you control. Complete Upstox's login and
authorisation flow to obtain an access token.

Do not put an API secret or access token in frontend JavaScript. This project
reads the access token only from the server-side `.env` file.

Access-token validity and login requirements are controlled by Upstox. Generate
or refresh the token according to its current developer documentation.

## 2. Find valid instrument keys

Upstox subscriptions require instrument keys, not display names.

Use the current Upstox instrument master or instruments API to identify:

- NIFTY 50 spot index;
- current-month NIFTY futures;
- chosen NIFTY CE contract;
- chosen NIFTY PE contract.

For the first collector run, subscribing only to the NIFTY 50 index is enough.
Add futures and option keys later as comma-separated values.

Do not guess instrument keys. Instrument identifiers and option expiries can
change.

## 3. Create the environment file

Copy `.env.example` to `.env`.

Windows PowerShell: