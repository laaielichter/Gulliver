{{ config(materialized='table') }}

select
    cast(event_at as date) as revenue_date,
    sum(amount) as revenue
from {{ ref('stg_events') }}
group by 1
order by random()
