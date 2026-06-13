"""Render the route and fuel stops to a Folium HTML map."""

import html as _html

import folium


def _popup_html(rows):
    items = "".join(f"<b>{_html.escape(k)}:</b> {_html.escape(v)}<br>" for k, v in rows)
    return folium.Popup(f"<div style='font-size:13px'>{items}</div>", max_width=300)


def build_map(route_coords, fuel_stops, start, finish) -> str:
    # Folium expects (lat, lng); OSRM gives (lng, lat).
    latlngs = [(lat, lng) for lng, lat in route_coords]

    m = folium.Map(tiles="OpenStreetMap", control_scale=True)
    folium.PolyLine(latlngs, color="#1f6feb", weight=4, opacity=0.8).add_to(m)

    folium.Marker(
        location=(start[0], start[1]),
        popup=_popup_html([("Start", start[2])]),
        icon=folium.Icon(color="green", icon="play", prefix="fa"),
    ).add_to(m)
    folium.Marker(
        location=(finish[0], finish[1]),
        popup=_popup_html([("Finish", finish[2])]),
        icon=folium.Icon(color="red", icon="flag-checkered", prefix="fa"),
    ).add_to(m)

    for fs in fuel_stops:
        station = fs.candidate.station
        folium.Marker(
            location=(fs.candidate.lat, fs.candidate.lng),
            popup=_popup_html([
                ("Station", station.name),
                ("Mile", f"{fs.mile_marker:.0f}"),
                ("Price", f"${fs.price_per_gallon:.2f}/gal"),
                ("Bought", f"{fs.gallons_purchased:.1f} gal"),
                ("Leg cost", f"${fs.leg_cost_usd:.2f}"),
            ]),
            tooltip=f"{station.name} (${fs.price_per_gallon:.2f})",
            icon=folium.Icon(color="orange", icon="gas-pump", prefix="fa"),
        ).add_to(m)

    lats = [p[0] for p in latlngs] + [start[0], finish[0]]
    lngs = [p[1] for p in latlngs] + [start[1], finish[1]]
    m.fit_bounds([(min(lats), min(lngs)), (max(lats), max(lngs))])

    return m.get_root().render()
