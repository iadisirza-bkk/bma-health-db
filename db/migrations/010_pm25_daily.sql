-- Migration 010: PM2.5 daily readings storage
-- Stores daily snapshots from ArcGIS air quality data per district.
-- ArcGIS provides one reading per Bangkok district (~50).
-- No PII involved — purely environmental data.

CREATE TABLE IF NOT EXISTS pm25_daily (
    dcode VARCHAR(4) NOT NULL REFERENCES ref_districts(dcode),
    reading_date DATE NOT NULL,
    avg_pm25 DECIMAL(6,2),
    max_pm25 DECIMAL(6,2),
    avg_aqi INTEGER,
    reading_count INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (dcode, reading_date)
);

CREATE INDEX IF NOT EXISTS idx_pm25_daily_date ON pm25_daily(reading_date);
CREATE INDEX IF NOT EXISTS idx_pm25_daily_dcode_date ON pm25_daily(dcode, reading_date);
