# Legacy Pyscript decommissioning

Do not delete legacy assets during initial migration. The new integration has a
read-only `adaptive_robovacs.decommission_report` service which lists known
legacy helpers and `pyscript.robovac_*` entities, plus automations/scripts that
reference the old scheduler.

After validation sign-off:

1. Export the report and confirm no external automation still needs an old
   Pyscript service.
2. Keep the ignored local legacy snapshot until rollback is no longer required.
3. Remove the legacy Pyscript module from Home Assistant and reload Pyscript.
4. Delete only the report's owned helper entities and persisted Pyscript status,
   schedule, activity, and store entities.
5. Remove the dedicated legacy dashboard after the dynamic dashboard is in use.
6. Restart Home Assistant and confirm the new scheduler restores its state.

The report intentionally does not delete anything. This protects unrelated
automations and makes final removal an explicit operator decision.
