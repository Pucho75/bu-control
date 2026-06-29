-- ============================================================
-- Rebuild v_lot_margin with full cost stack
-- SQLite-compatible (no LATERAL — uses correlated subqueries)
-- Run after migrate_cost_columns.py
-- ============================================================

DROP VIEW IF EXISTS v_lot_margin;

CREATE VIEW v_lot_margin AS
SELECT
    o.oda_code,
    s.id                        AS shipment_id,
    s.shipment_number,
    COALESCE(s.bl_number, o.oda_code || '-' || s.shipment_number) AS effective_lot_ref,
    p.code                      AS product_code,
    su.name                     AS supplier_name,
    s.vessel_name,
    s.bl_date,
    -- Use fx_rate_invoice from shipment for USD ODAs, fallback to price_eur_per_mt
    CASE WHEN o.currency = 'USD' AND s.fx_rate_invoice IS NOT NULL AND o.price_usd_per_mt IS NOT NULL
         THEN ROUND(o.price_usd_per_mt / s.fx_rate_invoice, 4)
         ELSE o.price_eur_per_mt
    END                         AS purchase_price_eur_per_mt,
    s.fx_rate_invoice,
    o.exchange_rate,
    -- Total MT
    SUM(COALESCE(c.actual_mt, c.nominal_mt))    AS total_mt,
    COUNT(DISTINCT c.id)                         AS num_containers,
    -- Sold MT
    COALESCE(SUM(sl_agg.mt_sold), 0)            AS sold_mt,
    -- Unsold MT
    SUM(COALESCE(c.actual_mt, c.nominal_mt)) - COALESCE(SUM(sl_agg.mt_sold), 0) AS unsold_mt,
    -- Realized margin
    COALESCE(SUM(sl_agg.margin_eur_total), 0)   AS realized_margin_eur,
    -- Realized landed cost per MT (weighted avg from snapshots)
    CASE WHEN COALESCE(SUM(sl_agg.mt_sold), 0) > 0
         THEN SUM(sl_agg.landed_cost_total) / SUM(sl_agg.mt_sold)
         ELSE NULL END                           AS realized_landed_cost_per_mt,
    -- Commission from delivery_costs (correlated subquery, ODA-date anchored)
    COALESCE((
        SELECT dc.commission FROM delivery_costs dc
        WHERE (dc.product_id  = o.product_id  OR dc.product_id  IS NULL)
          AND (dc.supplier_id = o.supplier_id OR dc.supplier_id IS NULL)
          AND (dc.paese       = o.paese        OR dc.paese       IS NULL)
          AND dc.valid_from <= o.order_date
        ORDER BY
            (CASE WHEN dc.product_id  IS NOT NULL THEN 4 ELSE 0 END +
             CASE WHEN dc.supplier_id IS NOT NULL THEN 2 ELSE 0 END +
             CASE WHEN dc.paese       IS NOT NULL THEN 1 ELSE 0 END) DESC,
            dc.valid_from DESC
        LIMIT 1
    ), 0)                                        AS commission_eur_per_mt,
    -- Log_in from delivery_costs (ODA-date anchored)
    COALESCE((
        SELECT dc.log_in FROM delivery_costs dc
        WHERE (dc.product_id  = o.product_id  OR dc.product_id  IS NULL)
          AND (dc.supplier_id = o.supplier_id OR dc.supplier_id IS NULL)
          AND (dc.paese       = o.paese        OR dc.paese       IS NULL)
          AND dc.valid_from <= o.order_date
        ORDER BY
            (CASE WHEN dc.product_id  IS NOT NULL THEN 4 ELSE 0 END +
             CASE WHEN dc.supplier_id IS NOT NULL THEN 2 ELSE 0 END +
             CASE WHEN dc.paese       IS NOT NULL THEN 1 ELSE 0 END) DESC,
            dc.valid_from DESC
        LIMIT 1
    ), 0)                                        AS log_in_eur_per_mt,
    -- Stoccaggio from delivery_costs (flat €/MT)
    COALESCE((
        SELECT dc.stoccaggio FROM delivery_costs dc
        WHERE (dc.product_id  = o.product_id  OR dc.product_id  IS NULL)
          AND (dc.supplier_id = o.supplier_id OR dc.supplier_id IS NULL)
          AND (dc.paese       = o.paese        OR dc.paese       IS NULL)
          AND dc.valid_from <= o.order_date
        ORDER BY
            (CASE WHEN dc.product_id  IS NOT NULL THEN 4 ELSE 0 END +
             CASE WHEN dc.supplier_id IS NOT NULL THEN 2 ELSE 0 END +
             CASE WHEN dc.paese       IS NOT NULL THEN 1 ELSE 0 END) DESC,
            dc.valid_from DESC
        LIMIT 1
    ), 0)                                        AS stoccaggio_eur_per_mt,
    -- Customs per MT (from containers)
    COALESCE(SUM(c.customs_cost_eur) / NULLIF(SUM(COALESCE(c.actual_mt, c.nominal_mt)), 0), 0) AS customs_per_mt,
    -- Transport to storage per MT (from containers)
    COALESCE(SUM(c.transport_to_storage_eur) / NULLIF(SUM(COALESCE(c.actual_mt, c.nominal_mt)), 0), 0) AS transport_to_storage_per_mt,
    -- Full carrying value of unsold MT
    (SUM(COALESCE(c.actual_mt, c.nominal_mt)) - COALESCE(SUM(sl_agg.mt_sold), 0))
        * (CASE WHEN o.currency = 'USD' AND s.fx_rate_invoice IS NOT NULL AND o.price_usd_per_mt IS NOT NULL
                THEN ROUND(o.price_usd_per_mt / s.fx_rate_invoice, 4)
                ELSE o.price_eur_per_mt END
           + COALESCE((
               SELECT dc.commission FROM delivery_costs dc
               WHERE (dc.product_id  = o.product_id  OR dc.product_id  IS NULL)
                 AND (dc.supplier_id = o.supplier_id OR dc.supplier_id IS NULL)
                 AND (dc.paese       = o.paese        OR dc.paese       IS NULL)
                 AND dc.valid_from <= o.order_date
               ORDER BY
                   (CASE WHEN dc.product_id  IS NOT NULL THEN 4 ELSE 0 END +
                    CASE WHEN dc.supplier_id IS NOT NULL THEN 2 ELSE 0 END +
                    CASE WHEN dc.paese       IS NOT NULL THEN 1 ELSE 0 END) DESC,
                   dc.valid_from DESC
               LIMIT 1
           ), 0)
           + COALESCE((
               SELECT dc.log_in FROM delivery_costs dc
               WHERE (dc.product_id  = o.product_id  OR dc.product_id  IS NULL)
                 AND (dc.supplier_id = o.supplier_id OR dc.supplier_id IS NULL)
                 AND (dc.paese       = o.paese        OR dc.paese       IS NULL)
                 AND dc.valid_from <= o.order_date
               ORDER BY
                   (CASE WHEN dc.product_id  IS NOT NULL THEN 4 ELSE 0 END +
                    CASE WHEN dc.supplier_id IS NOT NULL THEN 2 ELSE 0 END +
                    CASE WHEN dc.paese       IS NOT NULL THEN 1 ELSE 0 END) DESC,
                   dc.valid_from DESC
               LIMIT 1
           ), 0)
           + COALESCE((
               SELECT dc.stoccaggio FROM delivery_costs dc
               WHERE (dc.product_id  = o.product_id  OR dc.product_id  IS NULL)
                 AND (dc.supplier_id = o.supplier_id OR dc.supplier_id IS NULL)
                 AND (dc.paese       = o.paese        OR dc.paese       IS NULL)
                 AND dc.valid_from <= o.order_date
               ORDER BY
                   (CASE WHEN dc.product_id  IS NOT NULL THEN 4 ELSE 0 END +
                    CASE WHEN dc.supplier_id IS NOT NULL THEN 2 ELSE 0 END +
                    CASE WHEN dc.paese       IS NOT NULL THEN 1 ELSE 0 END) DESC,
                   dc.valid_from DESC
               LIMIT 1
           ), 0)
           + COALESCE(SUM(c.customs_cost_eur) / NULLIF(SUM(COALESCE(c.actual_mt, c.nominal_mt)), 0), 0)
           + COALESCE(SUM(c.transport_to_storage_eur) / NULLIF(SUM(COALESCE(c.actual_mt, c.nominal_mt)), 0), 0)
          )                                      AS unsold_carrying_value_eur
FROM shipments s
JOIN odas o       ON o.id  = s.oda_id  AND o.is_deleted = 0
JOIN products p   ON p.id  = o.product_id
JOIN suppliers su ON su.id = o.supplier_id
LEFT JOIN containers c ON c.shipment_id = s.id AND c.is_deleted = 0
LEFT JOIN (
    SELECT
        sl.container_id,
        SUM(sl.mt_drawn)                              AS mt_sold,
        SUM(sl.margin_eur_total)                      AS margin_eur_total,
        SUM(COALESCE(sl.landed_cost_eur_per_mt, 0) * sl.mt_drawn) AS landed_cost_total
    FROM sale_lots sl
    GROUP BY sl.container_id
) sl_agg ON sl_agg.container_id = c.id
WHERE s.is_deleted = 0
GROUP BY s.id
ORDER BY o.order_date DESC, s.shipment_number;
