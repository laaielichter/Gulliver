{{ config(materialized='view') }}

select *
from {{ source('app', 'events') }}
where date(event_at) >= '2025-01-01'
