-- =========================================================================
-- LiteAds Database Initialization – CPM CTV & In-App Video Only
-- =========================================================================
-- This script is run automatically when the PostgreSQL container starts.
-- Schema must match the SQLAlchemy ORM models in liteads/models/ad.py.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- -------------------------------------------------------------------------
-- Advertisers
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS advertisers (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255)  NOT NULL,
    company         VARCHAR(255),
    contact_email   VARCHAR(255),
    balance         DECIMAL(12,4) DEFAULT 0.0000 NOT NULL,
    daily_budget    DECIMAL(12,4) DEFAULT 0.0000 NOT NULL,
    status          INTEGER       DEFAULT 1      NOT NULL,
    created_at      TIMESTAMP     DEFAULT NOW()  NOT NULL,
    updated_at      TIMESTAMP     DEFAULT NOW()  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_advertiser_status ON advertisers(status);

-- -------------------------------------------------------------------------
-- Campaigns  (CPM-only, environment: 1=CTV, 2=INAPP, NULL=both)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS campaigns (
    id              SERIAL PRIMARY KEY,
    advertiser_id   INTEGER       NOT NULL REFERENCES advertisers(id) ON DELETE CASCADE,
    name            VARCHAR(255)  NOT NULL,
    description     TEXT,
    budget_daily    DECIMAL(12,4) DEFAULT 0.0000 NOT NULL,
    budget_total    DECIMAL(12,4) DEFAULT 0.0000 NOT NULL,
    spent_today     DECIMAL(12,4) DEFAULT 0.0000 NOT NULL,
    spent_total     DECIMAL(12,4) DEFAULT 0.0000 NOT NULL,
    bid_type        INTEGER       DEFAULT 1      NOT NULL,    -- 1 = CPM (only)
    bid_amount      DECIMAL(12,4) DEFAULT 0.0000 NOT NULL,    -- CPM price
    bid_floor       DECIMAL(10,4) DEFAULT 0.0000 NOT NULL,    -- Minimum CPM floor
    floor_config    JSONB,                                     -- Dynamic floor rules
    adomain         VARCHAR(255),                              -- Advertiser domain (for competitive separation)
    iab_categories  JSONB,                                     -- IAB content categories
    environment     INTEGER,                                   -- 1=CTV, 2=INAPP, NULL=both
    freq_cap_daily  INTEGER       DEFAULT 10     NOT NULL,
    freq_cap_hourly INTEGER       DEFAULT 3      NOT NULL,
    start_time      TIMESTAMP,
    end_time        TIMESTAMP,
    status          INTEGER       DEFAULT 1      NOT NULL,
    impressions     INTEGER       DEFAULT 0      NOT NULL,
    completions     INTEGER       DEFAULT 0      NOT NULL,
    clicks          INTEGER       DEFAULT 0      NOT NULL,
    created_at      TIMESTAMP     DEFAULT NOW()  NOT NULL,
    updated_at      TIMESTAMP     DEFAULT NOW()  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaign_advertiser  ON campaigns(advertiser_id);
CREATE INDEX IF NOT EXISTS idx_campaign_status      ON campaigns(status);
CREATE INDEX IF NOT EXISTS idx_campaign_schedule    ON campaigns(start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_campaign_environment ON campaigns(environment);

-- -------------------------------------------------------------------------
-- Creatives  (Video-only: CTV_VIDEO = 1, INAPP_VIDEO = 2)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS creatives (
    id                  SERIAL PRIMARY KEY,
    campaign_id         INTEGER       NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    title               VARCHAR(255)  NOT NULL,
    description         TEXT,
    video_url           VARCHAR(1024) NOT NULL,
    vast_url            VARCHAR(1024),
    companion_image_url VARCHAR(1024),
    landing_url         VARCHAR(1024) NOT NULL,
    creative_type       INTEGER       DEFAULT 2      NOT NULL,  -- 1=CTV_VIDEO, 2=INAPP_VIDEO
    duration            INTEGER       DEFAULT 30     NOT NULL,
    width               INTEGER       DEFAULT 1920   NOT NULL,
    height              INTEGER       DEFAULT 1080   NOT NULL,
    bitrate             INTEGER,
    mime_type           VARCHAR(50)   DEFAULT 'video/mp4' NOT NULL,
    skippable           BOOLEAN       DEFAULT TRUE   NOT NULL,
    skip_after          INTEGER       DEFAULT 5      NOT NULL,
    placement           INTEGER       DEFAULT 1      NOT NULL,  -- 1=PRE_ROLL, 2=MID_ROLL, 3=POST_ROLL
    status              INTEGER       DEFAULT 1      NOT NULL,
    quality_score       INTEGER       DEFAULT 80     NOT NULL,  -- 0-100 scale
    created_at          TIMESTAMP     DEFAULT NOW()  NOT NULL,
    updated_at          TIMESTAMP     DEFAULT NOW()  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_creative_campaign ON creatives(campaign_id);
CREATE INDEX IF NOT EXISTS idx_creative_status   ON creatives(status);
CREATE INDEX IF NOT EXISTS idx_creative_type     ON creatives(creative_type);

-- -------------------------------------------------------------------------
-- Targeting Rules
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS targeting_rules (
    id          SERIAL PRIMARY KEY,
    campaign_id INTEGER     NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    rule_type   VARCHAR(50) NOT NULL,
    rule_value  JSONB       NOT NULL,
    is_include  BOOLEAN     DEFAULT TRUE NOT NULL,
    created_at  TIMESTAMP   DEFAULT NOW() NOT NULL,
    updated_at  TIMESTAMP   DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_targeting_campaign ON targeting_rules(campaign_id);
CREATE INDEX IF NOT EXISTS idx_targeting_type     ON targeting_rules(rule_type);

-- -------------------------------------------------------------------------
-- Ad Events  (VAST video events + win/billing)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ad_events (
    id             SERIAL PRIMARY KEY,
    request_id     VARCHAR(64)    NOT NULL,
    campaign_id    INTEGER        REFERENCES campaigns(id) ON DELETE SET NULL,
    creative_id    INTEGER        REFERENCES creatives(id) ON DELETE SET NULL,
    event_type     INTEGER        NOT NULL,
    event_time     TIMESTAMP      NOT NULL,
    user_id        VARCHAR(64),
    ip_address     VARCHAR(45),
    cost           DECIMAL(10,6)  DEFAULT 0.000000 NOT NULL,
    win_price      DECIMAL(10,6)  DEFAULT 0.000000 NOT NULL,
    adomain        VARCHAR(255),
    source_name    VARCHAR(255),
    bundle_id      VARCHAR(255),
    country_code   VARCHAR(3),
    video_position INTEGER,
    environment    INTEGER,                        -- 1=CTV, 2=INAPP
    created_at     TIMESTAMP      DEFAULT NOW() NOT NULL,
    updated_at     TIMESTAMP      DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_request   ON ad_events(request_id);
CREATE INDEX IF NOT EXISTS idx_event_campaign  ON ad_events(campaign_id);
CREATE INDEX IF NOT EXISTS idx_event_time      ON ad_events(event_time);
CREATE INDEX IF NOT EXISTS idx_event_type_time ON ad_events(event_type, event_time);
CREATE INDEX IF NOT EXISTS idx_event_adomain   ON ad_events(adomain);
CREATE INDEX IF NOT EXISTS idx_event_source    ON ad_events(source_name);
CREATE INDEX IF NOT EXISTS idx_event_bundle    ON ad_events(bundle_id);
CREATE INDEX IF NOT EXISTS idx_event_country   ON ad_events(country_code);
CREATE INDEX IF NOT EXISTS ix_ad_events_campaign_type_time ON ad_events(campaign_id, event_type, event_time);

-- -------------------------------------------------------------------------
-- Hourly Stats  (Video metrics for fill-rate / VTR optimisation)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hourly_stats (
    id                SERIAL PRIMARY KEY,
    campaign_id       INTEGER        NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    stat_hour         TIMESTAMP      NOT NULL,
    ad_requests       INTEGER        DEFAULT 0 NOT NULL,
    ad_opportunities  INTEGER        DEFAULT 0 NOT NULL,
    wins              INTEGER        DEFAULT 0 NOT NULL,
    impressions       INTEGER        DEFAULT 0 NOT NULL,
    starts            INTEGER        DEFAULT 0 NOT NULL,
    first_quartiles   INTEGER        DEFAULT 0 NOT NULL,
    midpoints         INTEGER        DEFAULT 0 NOT NULL,
    third_quartiles   INTEGER        DEFAULT 0 NOT NULL,
    completions       INTEGER        DEFAULT 0 NOT NULL,
    clicks            INTEGER        DEFAULT 0 NOT NULL,
    skips             INTEGER        DEFAULT 0 NOT NULL,
    spend             DECIMAL(12,4)  DEFAULT 0.0000 NOT NULL,
    win_price_sum     DECIMAL(12,4)  DEFAULT 0.0000 NOT NULL,
    vtr               DECIMAL(8,6)   DEFAULT 0.000000 NOT NULL,
    CONSTRAINT uq_hourly_stat_campaign_hour UNIQUE (campaign_id, stat_hour)
);

CREATE INDEX IF NOT EXISTS idx_stats_campaign_hour ON hourly_stats(campaign_id, stat_hour);
CREATE INDEX IF NOT EXISTS idx_stats_hour          ON hourly_stats(stat_hour);

-- =========================================================================
-- Seed Data – CTV & In-App Video Campaigns
-- =========================================================================
-- environment: 1=CTV, 2=INAPP  |  placement: 1=PRE_ROLL, 2=MID_ROLL, 3=POST_ROLL

INSERT INTO advertisers (name, company, balance, daily_budget, status) VALUES
    ('CTV Demo Advertiser',    'StreamCo Inc.',     50000.0000, 1000.0000, 1),
    ('InApp Video Advertiser', 'GameMedia Corp.',   30000.0000,  500.0000, 1);

-- CTV campaigns  (environment = 1)
INSERT INTO campaigns (advertiser_id, name, description, budget_daily, budget_total, bid_type, bid_amount, environment, status) VALUES
    (1, 'CTV Pre-Roll – Premium',   'Premium CTV pre-roll campaign',  500.0000, 10000.0000, 1, 12.0000, 1, 1),
    (1, 'CTV Mid-Roll – Sports',    'Sports mid-roll campaign',       300.0000,  6000.0000, 1,  8.0000, 1, 1);

-- InApp campaigns  (environment = 2)
INSERT INTO campaigns (advertiser_id, name, description, budget_daily, budget_total, bid_type, bid_amount, environment, status) VALUES
    (2, 'InApp Video – Casual Games', 'Casual game rewarded video',  200.0000,  4000.0000, 1, 6.0000, 2, 1),
    (2, 'InApp Video – Streaming',    'Streaming app interstitial',  250.0000,  5000.0000, 1, 7.5000, 2, 1);

-- Creatives  (CTV — creative_type 1, placement 1=PRE_ROLL / 2=MID_ROLL)
INSERT INTO creatives (campaign_id, title, description, video_url, companion_image_url, landing_url,
                       creative_type, duration, width, height, bitrate, mime_type,
                       skippable, skip_after, placement, status, quality_score) VALUES
    (1, 'CTV 30s Pre-Roll', 'Premium CTV pre-roll ad',
     'https://cdn.example.com/video/ctv_preroll_30s.mp4', 'https://cdn.example.com/img/companion_300x250.png',
     'https://example.com/landing/ctv1', 1, 30, 1920, 1080, 5000, 'video/mp4',
     TRUE, 5, 1, 1, 85),
    (2, 'CTV 15s Mid-Roll', 'Sports mid-roll ad',
     'https://cdn.example.com/video/ctv_midroll_15s.mp4', NULL,
     'https://example.com/landing/ctv2', 1, 15, 1920, 1080, 4000, 'video/mp4',
     FALSE, 0, 2, 1, 80);

-- Creatives  (InApp — creative_type 2, placement 1=PRE_ROLL)
INSERT INTO creatives (campaign_id, title, description, video_url, companion_image_url, landing_url,
                       creative_type, duration, width, height, bitrate, mime_type,
                       skippable, skip_after, placement, status, quality_score) VALUES
    (3, 'InApp 15s Rewarded', 'Rewarded video for casual games',
     'https://cdn.example.com/video/inapp_reward_15s.mp4', 'https://cdn.example.com/img/inapp_comp.png',
     'https://example.com/landing/inapp1', 2, 15, 1280, 720, 2500, 'video/mp4',
     FALSE, 0, 1, 1, 75),
    (4, 'InApp 30s Interstitial', 'Streaming app interstitial',
     'https://cdn.example.com/video/inapp_interstitial_30s.mp4', NULL,
     'https://example.com/landing/inapp2', 2, 30, 1280, 720, 3000, 'video/mp4',
     TRUE, 5, 1, 1, 78);

-- Targeting Rules
INSERT INTO targeting_rules (campaign_id, rule_type, rule_value, is_include) VALUES
    -- CTV campaign 1: target Roku + Fire TV in US
    (1, 'environment', '{"values": ["ctv"]}', TRUE),
    (1, 'device',      '{"os": ["roku", "firetv", "tvos"]}', TRUE),
    (1, 'geo',         '{"countries": ["US"], "dma": ["501", "803"]}', TRUE),
    -- CTV campaign 2: target all CTV devices
    (2, 'environment', '{"values": ["ctv"]}', TRUE),
    (2, 'content_genre', '{"values": ["sports", "entertainment"]}', TRUE),
    -- InApp campaign 3: target mobile devices
    (3, 'environment', '{"values": ["inapp"]}', TRUE),
    (3, 'device',      '{"os": ["android", "ios"]}', TRUE),
    -- InApp campaign 4: target all InApp
    (4, 'environment', '{"values": ["inapp"]}', TRUE);

-- =========================================================================
-- Migration for existing deployments – add new CTV columns safely
-- =========================================================================
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='campaigns' AND column_name='bid_floor') THEN
        ALTER TABLE campaigns ADD COLUMN bid_floor DECIMAL(10,4) DEFAULT 0.0000 NOT NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='campaigns' AND column_name='floor_config') THEN
        ALTER TABLE campaigns ADD COLUMN floor_config JSONB;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='campaigns' AND column_name='adomain') THEN
        ALTER TABLE campaigns ADD COLUMN adomain VARCHAR(255);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='campaigns' AND column_name='iab_categories') THEN
        ALTER TABLE campaigns ADD COLUMN iab_categories JSONB;
    END IF;
END $$;

-- =========================================================================
-- Supply / Demand management tables
-- =========================================================================

-- -------------------------------------------------------------------------
-- Supply Tags – Publisher-facing VAST tag configurations
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supply_tags (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255)  NOT NULL,
    description     TEXT,
    slot_id         VARCHAR(100)  NOT NULL UNIQUE,
    bid_floor       DECIMAL(10,4) DEFAULT 0.0000 NOT NULL,
    margin_pct      DECIMAL(6,2)  DEFAULT 0.00   NOT NULL,
    environment     INTEGER,
    min_duration    INTEGER       DEFAULT 5      NOT NULL,
    max_duration    INTEGER       DEFAULT 30     NOT NULL,
    width           INTEGER       DEFAULT 1920   NOT NULL,
    height          INTEGER       DEFAULT 1080   NOT NULL,
    status          INTEGER       DEFAULT 1      NOT NULL,
    created_at      TIMESTAMP     DEFAULT NOW()  NOT NULL,
    updated_at      TIMESTAMP     DEFAULT NOW()  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_tag_slot   ON supply_tags(slot_id);
CREATE INDEX IF NOT EXISTS idx_supply_tag_status ON supply_tags(status);

-- -------------------------------------------------------------------------
-- Demand Endpoints – Third-party OpenRTB endpoints (DSPs / bridge servers)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS demand_endpoints (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255)   NOT NULL,
    description     TEXT,
    endpoint_url    VARCHAR(1024)  NOT NULL,
    bid_floor       DECIMAL(10,4)  DEFAULT 0.0000 NOT NULL,
    margin_pct      DECIMAL(6,2)   DEFAULT 0.00   NOT NULL,
    timeout_ms      INTEGER        DEFAULT 500    NOT NULL,
    qps_limit       INTEGER        DEFAULT 0      NOT NULL,
    status          INTEGER        DEFAULT 1      NOT NULL,
    created_at      TIMESTAMP      DEFAULT NOW()  NOT NULL,
    updated_at      TIMESTAMP      DEFAULT NOW()  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_demand_ep_status ON demand_endpoints(status);

-- -------------------------------------------------------------------------
-- Demand VAST Tags – Third-party demand VAST tag sources
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS demand_vast_tags (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(255)   NOT NULL,
    description     TEXT,
    vast_url        VARCHAR(2048)  NOT NULL,
    bid_floor       DECIMAL(10,4)  DEFAULT 0.0000 NOT NULL,
    margin_pct      DECIMAL(6,2)   DEFAULT 0.00   NOT NULL,
    cpm_value       DECIMAL(10,4)  DEFAULT 0.0000 NOT NULL,
    status          INTEGER        DEFAULT 1      NOT NULL,
    created_at      TIMESTAMP      DEFAULT NOW()  NOT NULL,
    updated_at      TIMESTAMP      DEFAULT NOW()  NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_demand_vast_status ON demand_vast_tags(status);

-- -------------------------------------------------------------------------
-- Supply ↔ Demand Mapping (many-to-many with priority/weight)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS supply_demand_mappings (
    id                  SERIAL PRIMARY KEY,
    supply_tag_id       INTEGER NOT NULL REFERENCES supply_tags(id) ON DELETE CASCADE,
    demand_endpoint_id  INTEGER REFERENCES demand_endpoints(id) ON DELETE CASCADE,
    demand_vast_tag_id  INTEGER REFERENCES demand_vast_tags(id) ON DELETE CASCADE,
    priority            INTEGER DEFAULT 1   NOT NULL,
    weight              INTEGER DEFAULT 100 NOT NULL,
    status              INTEGER DEFAULT 1   NOT NULL,
    created_at          TIMESTAMP DEFAULT NOW() NOT NULL,
    updated_at          TIMESTAMP DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sdm_supply   ON supply_demand_mappings(supply_tag_id);
CREATE INDEX IF NOT EXISTS idx_sdm_demand   ON supply_demand_mappings(demand_endpoint_id);
CREATE INDEX IF NOT EXISTS idx_sdm_vast     ON supply_demand_mappings(demand_vast_tag_id);

-- =========================================================================
-- Seed Supply / Demand Data
-- =========================================================================
INSERT INTO supply_tags (name, slot_id, bid_floor, margin_pct, environment, min_duration, max_duration)
VALUES
    ('CTV Pre-Roll Default', 'ctv_preroll', 3.0000, 20.00, 1, 5, 30),
    ('CTV Mid-Roll Sports',  'ctv_midroll', 5.0000, 15.00, 1, 10, 60),
    ('InApp Rewarded Video',  'inapp_reward', 2.0000, 25.00, 2, 5, 30);

INSERT INTO demand_endpoints (name, endpoint_url, bid_floor, margin_pct, timeout_ms)
VALUES
    ('Primary DSP',     'https://dsp1.example.com/ortb/bid', 2.0000, 10.00, 300),
    ('Secondary Bridge', 'https://bridge.example.com/bid',   1.5000, 15.00, 500);

INSERT INTO demand_vast_tags (name, vast_url, bid_floor, margin_pct, cpm_value)
VALUES
    ('Fallback VAST Network', 'https://vastnet.example.com/vast?s=[SLOT]&w=[WIDTH]&h=[HEIGHT]', 1.0000, 20.00, 4.0000);

INSERT INTO supply_demand_mappings (supply_tag_id, demand_endpoint_id, priority, weight)
VALUES
    (1, 1, 1, 100),
    (1, 2, 2, 100),
    (2, 1, 1, 100);

INSERT INTO supply_demand_mappings (supply_tag_id, demand_vast_tag_id, priority, weight)
VALUES
    (1, 1, 3, 100),
    (3, 1, 1, 100);

DO $$
BEGIN
    RAISE NOTICE 'LiteAds database initialized – CPM CTV & In-App Video Only!';
END $$;
