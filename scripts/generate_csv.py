import argparse
import csv
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Generate reproducible synthetic inventory; no random prices."
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--rows", type=int, default=2500)
    args = parser.parse_args()
    if not 1 <= args.rows <= 50000:
        parser.error("Use 1–50000 records.")
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sku", "name", "price", "qty", "category"])
        for index in range(1, args.rows + 1):
            writer.writerow(
                [
                    f"DEMO-{index:06}",
                    f"Supplier product {index}",
                    f"{index % 100}.{index % 100:02}",
                    index % 1000,
                    "Demo",
                ]
            )
    print(f"Wrote {args.rows} records to {args.output}")


if __name__ == "__main__":
    main()
