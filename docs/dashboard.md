# Dynamic dashboard

The integration serves its card after Home Assistant starts. Add this resource
in Settings > Dashboards > Resources:

```yaml
url: /api/adaptive_robovacs/frontend/adaptive-robovacs-dashboard.js
type: module
```

Then create a dashboard from `dashboard/example-dashboard.yaml`:

```yaml
type: custom:adaptive-robovacs-dashboard
```

To keep unmapped areas out of a particular dashboard, optionally pass their
Home Assistant area IDs. This affects presentation only; disable those rooms'
scheduler switches as well so they cannot be scheduled:

```yaml
type: custom:adaptive-robovacs-dashboard
hidden_area_ids:
  - laundry
  - garage
```

The card discovers Adaptive RoboVacs entities through their integration
attributes. It therefore updates as a room, vacuum, or supported robot control
appears. It presents Party Mode and observe-only state, active robot activity,
mode, current room, room controls, next clean status, safe estimated start,
last-cleaned time, occupancy source, and native map status.

The standalone copy in `dashboard/adaptive-robovacs-dashboard.js` is retained
for dashboards that prefer `/local/` resources, but the integration-served URL
is the supported default.
