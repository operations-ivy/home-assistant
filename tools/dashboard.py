import json, sys
DS = {"type": "prometheus", "uid": "prometheus"}
F = lambda m: f"{m} * 9 / 5 + 32"   # HA exports °C; the house runs in °F
# max()/sum() over HA series: a restart is a new pod, so a new series.
TH = 'entity="climate.thermostat"'
panels, pid = [], [0]

def nid():
    pid[0] += 1
    return pid[0]

def tgt(expr, legend="", instant=False, ref="A"):
    t = {"refId": ref, "datasource": DS, "expr": expr, "legendFormat": legend}
    if instant: t["instant"] = True
    return t

def row(title, y):
    panels.append({"type": "row", "title": title, "collapsed": False, "panels": [],
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "id": nid()})

def stat(title, desc, expr, x, y, unit="none", mappings=None, steps=None, decimals=None, legend="", text="auto", color="none", w=4):
    d = {"unit": unit, "color": {"mode": "thresholds"},
         "thresholds": {"mode": "absolute", "steps": steps or [{"color": "text", "value": None}]},
         "mappings": [{"type": "value", "options": mappings}] if mappings else []}
    if decimals is not None: d["decimals"] = decimals
    panels.append({"type": "stat", "title": title, "description": desc, "datasource": DS,
                   "targets": [tgt(expr, legend, True)], "fieldConfig": {"defaults": d, "overrides": []},
                   "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                               "colorMode": color, "graphMode": "none", "textMode": text,
                               "justifyMode": "auto", "orientation": "auto"},
                   "gridPos": {"h": 4, "w": w, "x": x, "y": y}, "id": nid()})

def series(title, desc, targets, x, y, w, h, unit="none", decimals=None, step=False, bars=False, minzero=True, legend_calcs=None):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 10 if not bars else 60,
              "showPoints": "never", "spanNulls": False, "lineInterpolation": "stepAfter" if step else "smooth",
              "stacking": {"mode": "normal" if bars else "none", "group": "A"}}
    if minzero: custom["axisSoftMin"] = 0
    d = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if decimals is not None: d["decimals"] = decimals
    panels.append({"type": "timeseries", "title": title, "description": desc, "datasource": DS,
                   "targets": [tgt(e, l, ref="ABCDEFGH"[i]) for i, (e, l) in enumerate(targets)],
                   "fieldConfig": {"defaults": d, "overrides": []},
                   "options": {"legend": {"displayMode": "list", "placement": "bottom", "calcs": legend_calcs or []},
                               "tooltip": {"mode": "multi", "sort": "desc"}},
                   "gridPos": {"h": h, "w": w, "x": x, "y": y}, "id": nid()})

def timeline(title, desc, labels, metric, label, x, y, w, h):
    expr = " + ".join(f'{i} * max({metric}{{{TH},{label}="{k}"}})' for i, (k, _, _) in enumerate(labels, 1))
    mappings = {str(i): {"text": text, "color": colour, "index": i - 1} for i, (_, text, colour) in enumerate(labels, 1)}
    panels.append({"type": "state-timeline", "title": title, "description": desc, "datasource": DS,
                   "targets": [tgt(expr, title)],
                   "fieldConfig": {"defaults": {"color": {"mode": "thresholds"},
                                                "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
                                                "mappings": [{"type": "value", "options": mappings}],
                                                "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
                   "options": {"showValue": "auto", "mergeValues": True, "rowHeight": 0.8, "alignValue": "left",
                               "legend": {"showLegend": False}},
                   "gridPos": {"h": h, "w": w, "x": x, "y": y}, "id": nid()})

ACTIONS = [("idle", "Idle", "text"), ("cooling", "Cooling", "blue"), ("heating", "Heating", "orange"),
           ("fan", "Fan only", "green"), ("off", "Off", "dark-gray"), ("drying", "Drying", "purple"),
           ("defrosting", "Defrosting", "purple"), ("preheating", "Preheating", "orange")]
MODES = [("off", "Off", "dark-gray"), ("heat", "Heat", "orange"), ("cool", "Cool", "blue"), ("heat_cool", "Auto", "purple")]

row("The ecobee (read-only: HA watches, never changes it)", 0)
stat("Inside", "The ecobee's own sensor.", F(f"max(hass_climate_current_temperature_celsius{{{TH}}})"), 0, 1, "fahrenheit", decimals=1)
stat("Target", "The setpoint (single-setpoint modes).", F(f"max(hass_climate_target_temperature_celsius{{{TH}}})"), 4, 1, "fahrenheit", decimals=0)
stat("Humidity", "Indoor relative humidity.", 'max(hass_sensor_humidity_percent{entity="sensor.my_ecobee_current_humidity"})', 8, 1, "percent", decimals=0)
stat("Mode", "What the ecobee is set to.", f'max by (mode) (hass_climate_mode{{{TH}}}) == 1', 12, 1,
     legend="{{mode}}", text="name")
stat("Doing now", "What the ecobee is doing.", f'max by (action) (hass_climate_action{{{TH}}}) == 1', 16, 1,
     legend="{{action}}", text="name")
stat("Outside", "From the LCARS panel's weather (National Weather Service, or a backup).",
     "lcars_weather_temperature_fahrenheit", 20, 1, "fahrenheit", decimals=1)

series("Temperatures", "Inside (the ecobee, downstairs), the setpoint, outside, and the bedroom (brick1982's sensor), all in °F.",
       [(F(f"max(hass_climate_current_temperature_celsius{{{TH}}})"), "Inside"),
        (F(f"max(hass_climate_target_temperature_celsius{{{TH}}})"), "Target"),
        ("lcars_weather_temperature_fahrenheit", "Outside"),
        ("lcars_room_temperature_fahrenheit", "Bedroom")], 0, 5, 16, 9, "fahrenheit", 1, minzero=False)
stat("Cooling, 24h", "Hours the ecobee spent cooling in the last 24 hours.",
     f'sum_over_time((max(hass_climate_action{{{TH},action="cooling"}}) or vector(0))[24h:1m]) / 60', 16, 5, "h", decimals=1)
stat("Heating, 24h", "Hours the ecobee spent heating in the last 24 hours.",
     f'sum_over_time((max(hass_climate_action{{{TH},action="heating"}}) or vector(0))[24h:1m]) / 60', 20, 5, "h", decimals=1)
stat("Fan only, 24h", "Hours with only the fan running in the last 24 hours.",
     f'sum_over_time((max(hass_climate_action{{{TH},action="fan"}}) or vector(0))[24h:1m]) / 60', 16, 9, "h", decimals=1)
stat("Setpoint changes, 24h", "Times the setpoint changed in the last 24 hours (at the ecobee, in its app, or a schedule).",
     f'sum(changes(hass_climate_target_temperature_celsius{{{TH}}}[24h]))', 20, 9, decimals=0)
panels[-1]["gridPos"]["h"] = 5
panels[-2]["gridPos"]["h"] = 5
timeline("What it's doing", "The ecobee's activity over time.", ACTIONS, "hass_climate_action", "action", 0, 14, 24, 5)
timeline("Mode", "What the ecobee was set to.", MODES, "hass_climate_mode", "mode", 0, 19, 12, 5)
series("Humidity", "Indoor relative humidity: the ecobee's (downstairs) and the bedroom's (brick1982's sensor).",
       [('max(hass_sensor_humidity_percent{entity="sensor.my_ecobee_current_humidity"})', "Ecobee"),
        ("lcars_room_humidity_percent", "Bedroom")],
       12, 19, 12, 5, "percent", 0)

row("Home Assistant", 24)
stat("Scrape", "Prometheus reaching HA's /api/prometheus (the Monitoring user's token).",
     'max(up{namespace="home-assistant"})', 0, 25, mappings={"0": {"text": "Down", "color": "red", "index": 0},
                                                             "1": {"text": "Up", "color": "green", "index": 1}}, color="background")
stat("Panel's link", "The LCARS panel on brick1982 reaching HA (the Panel user's token).",
     "lcars_ha_up", 4, 25, mappings={"0": {"text": "Down", "color": "red", "index": 0},
                                     "1": {"text": "Up", "color": "green", "index": 1}}, color="background")
stat("Unavailable entities", "Entities HA reports unavailable (some, like backup times before the first backup, always are).",
     "count(max by (entity) (hass_entity_available) == 0) or vector(0)", 8, 25, decimals=0)
stat("State changes, 1h", "Entity state changes in the last hour.", "sum(increase(hass_state_change_total[1h]))", 12, 25, decimals=0)
stat("Memory", "The HA pod's working set.", 'sum(container_memory_working_set_bytes{namespace="home-assistant",container="home-assistant"})',
     16, 25, "bytes", decimals=0)
stat("CPU", "The HA pod's CPU, in cores.", 'sum(rate(container_cpu_usage_seconds_total{namespace="home-assistant",container="home-assistant"}[5m]))',
     20, 25, "none", decimals=2)
series("State changes by domain", "Per hour.", [("sum by (domain) (increase(hass_state_change_total[1h]))", "{{domain}}")],
       0, 29, 12, 8, bars=True, decimals=0)
series("Pod memory", "The HA pod's working set.", [('sum(container_memory_working_set_bytes{namespace="home-assistant",container="home-assistant"})', "Memory")],
       12, 29, 6, 8, "bytes")
series("Pod CPU", "Cores used.", [('sum(rate(container_cpu_usage_seconds_total{namespace="home-assistant",container="home-assistant"}[5m]))', "CPU")],
       18, 29, 6, 8, decimals=2)
panels.append({"type": "table", "title": "Unavailable entities", "description": "Entities HA reports unavailable right now.",
               "datasource": DS, "targets": [{**tgt("max by (entity, friendly_name, domain) (hass_entity_available) == 0", "", True), "format": "table"}],
               "transformations": [{"id": "organize", "options": {
                   "excludeByName": {"Time": True, "Value": True, "__name__": True, "container": True, "endpoint": True,
                                     "instance": True, "job": True, "namespace": True, "pod": True, "prometheus": True},
                   "renameByName": {"entity": "Entity", "friendly_name": "Name", "domain": "Domain"}}}],
               "fieldConfig": {"defaults": {}, "overrides": []}, "options": {"showHeader": True},
               "gridPos": {"h": 7, "w": 24, "x": 0, "y": 37}, "id": nid()})

# The router, through HA's UPnP integration ("Verizon Router"). Its rates
# are KiB/s; shown in bits/s like an internet plan. The external IP is a
# state, not a number, and stays off this (public) dashboard.
WAN = 'entity="binary_sensor.verizon_router_wan_status"'
DOWN = 'max(hass_sensor_data_rate_kib_per_s{entity="sensor.verizon_router_download_speed"})'
UP = 'max(hass_sensor_data_rate_kib_per_s{entity="sensor.verizon_router_upload_speed"})'
BITS = " * 1024 * 8"
row("Internet (the router, over UPnP)", 44)
stat("WAN", "The router's internet connection.", f"max(hass_binary_sensor_state{{{WAN}}})", 0, 45,
     mappings={"0": {"text": "Down", "color": "red", "index": 0}, "1": {"text": "Up", "color": "green", "index": 1}},
     color="background")
stat("Download now", "Whole-house download rate, from the router.", DOWN + BITS, 4, 45, "bps", decimals=1)
stat("Upload now", "Whole-house upload rate, from the router.", UP + BITS, 8, 45, "bps", decimals=1)
stat("Downloaded, 24h", "Data downloaded in the last 24 hours (the rate, added up minute by minute).",
     f"sum_over_time(({DOWN})[24h:1m]) * 60 * 1024", 12, 45, "bytes", decimals=1)
stat("Uploaded, 24h", "Data uploaded in the last 24 hours (the rate, added up minute by minute).",
     f"sum_over_time(({UP})[24h:1m]) * 60 * 1024", 16, 45, "bytes", decimals=1)
stat("WAN drops, 24h", "Times the connection went down in the last 24 hours.",
     f"sum(changes(hass_binary_sensor_state{{{WAN}}}[24h])) / 2", 20, 45, decimals=0)
series("Throughput", "Whole-house download and upload, from the router.",
       [(DOWN + BITS, "Download"), (UP + BITS, "Upload")], 0, 49, 24, 8, "bps", 1)
panels.append({"type": "state-timeline", "title": "WAN status", "description": "Up or down over time.",
               "datasource": DS, "targets": [tgt(f"max(hass_binary_sensor_state{{{WAN}}})", "WAN")],
               "fieldConfig": {"defaults": {"color": {"mode": "thresholds"},
                                            "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
                                            "mappings": [{"type": "value", "options": {
                                                "0": {"text": "Down", "color": "red", "index": 0},
                                                "1": {"text": "Up", "color": "green", "index": 1}}}],
                                            "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
               "options": {"showValue": "auto", "mergeValues": True, "rowHeight": 0.8, "alignValue": "left",
                           "legend": {"showLegend": False}},
               "gridPos": {"h": 3, "w": 24, "x": 0, "y": 57}, "id": nid()})

dash = {"annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True,
                                  "hide": True, "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"}]},
        "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1, "id": None, "links": [], "refresh": "1m",
        "schemaVersion": 41, "tags": ["home-assistant", "ecobee", "internet", "brick2000"], "templating": {"list": []},
        "time": {"from": "now-24h", "to": "now"}, "timepicker": {}, "timezone": "browser",
        "title": "Home Assistant: the ecobee and the internet", "uid": "home-assistant", "version": 1, "panels": panels}
body = json.dumps(dash, indent=2, ensure_ascii=False)
out = sys.argv[1]
with open(out, "w") as f:
    f.write("""# Grafana dashboard for Home Assistant's metrics (podmonitor.yaml) and the
# ecobee; Grafana's sidecar loads any ConfigMap labelled grafana_dashboard, in
# any namespace. Generated by tools/dashboard.py: edit that, rerun it, apply.
apiVersion: v1
kind: ConfigMap
metadata:
  name: home-assistant-dashboard
  namespace: home-assistant
  labels:
    grafana_dashboard: "1"
data:
  home-assistant-dashboard.json: |
""" + "\n".join("    " + l for l in body.splitlines()) + "\n")
exprs = [t["expr"] for p in panels for t in p.get("targets", [])]
# The queries, for checking against live Prometheus: --exprs prints them.
if "--exprs" in sys.argv:
    print("\n".join(exprs))
print(len(panels), "panels,", len(exprs), "queries")
