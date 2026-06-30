"""List active wildfires."""

from libwatchduty import WatchDutyClient


def main() -> None:
    """Print up to 25 active wildfires with acreage and containment."""
    c = WatchDutyClient()
    fires = c.list_geo_events(types=["wildfire"], active_only=True)
    print(f"{len(fires)} active wildfires")
    for f in fires[:25]:
        d = f.get("data") or {}
        print(
            f"  {f['id']:>7}  {f['name'][:45]:<45}  "
            f"{(d.get('acreage') or 0):>7} ac  "
            f"{(d.get('containment') if d.get('containment') is not None else '-')!s:>4}% cont"
        )


if __name__ == "__main__":
    main()
