import csv
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from time import perf_counter
from typing import Any

import yaml

# --- EXCEPTIONS ---
class ApplicationError(Exception):
    """Base application exception."""

class ConfigurationError(ApplicationError):
    """Configuration is invalid."""

class DataError(ApplicationError):
    """Base data exception."""

class DataImportError(DataError):
    """Input data cannot be imported."""

class DataExportError(DataError):
    """Output cannot be written."""

class RecordValidationError(DataError):
    def __init__(self, message: str, *, line_number: int | None = None, field: str | None = None) -> None:
        super().__init__(message)
        self.line_number = line_number
        self.field = field

# --- MODELS ---
@dataclass(frozen=True, slots=True)
class Product:
    product_id: str
    name: str
    category: str
    price: float
    quantity: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class ProcessingConfig:
    min_stock: int
    allowed_categories: list[str]
    skip_invalid: bool

@dataclass(frozen=True, slots=True)
class AppConfig:
    input_path: Path
    output_path: Path
    processing: ProcessingConfig
    log_level: str

# --- UTILS ---
@contextmanager
def logged_operation(operation: str):
    logger = logging.getLogger(__name__)
    logger.info(f"{operation} started")
    start = perf_counter()
    try:
        yield
    except Exception:
        logger.exception(f"{operation} failed")
        raise
    else:
        elapsed = perf_counter() - start
        logger.info(f"{operation} finished in {elapsed:.6f} s")

# --- SERVICES ---
def load_config(path: Path) -> AppConfig:
    try:
        with path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
        return AppConfig(
            input_path=Path(raw["input"]["path"]),
            output_path=Path(raw["output"]["path"]),
            processing=ProcessingConfig(
                min_stock=int(raw["processing"]["min_stock"]),
                allowed_categories=list(raw["processing"]["allowed_categories"]),
                skip_invalid=bool(raw["processing"]["skip_invalid"]),
            ),
            log_level=str(raw["logging"]["level"]),
        )
    except Exception as e:
        raise ConfigurationError("Invalid config") from e

def parse_product(row: dict[str, str], line_number: int, allowed_cats: list[str]) -> Product:
    try:
        pid = row["product_id"].strip()
        name = row["name"].strip()
        cat = row["category"].strip()
        price = float(row["price"])
        qty = int(row["quantity"])
        
        if not pid or not name: raise ValueError("ID/Name missing")
        if cat not in allowed_cats: raise ValueError(f"Category {cat} not allowed")
        if price < 0 or qty < 0: raise ValueError("Negative values")
        
        return Product(pid, name, cat, price, qty)
    except (ValueError, KeyError) as e:
        raise RecordValidationError(str(e), line_number=line_number) from e

def run_import_export(config: AppConfig):
    # Setup Logging
    logging.basicConfig(level=config.log_level.upper(), format="%(levelname)s | %(message)s")
    logger = logging.getLogger(__name__)
    
    stats = {"total": 0, "valid": 0, "invalid": 0}
    valid_products = []
    
    with logged_operation("Product Import"):
        with config.input_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=2):
                stats["total"] += 1
                try:
                    prod = parse_product(row, i, config.processing.allowed_categories)
                    valid_products.append(prod)
                    stats["valid"] += 1
                    if prod.quantity < config.processing.min_stock:
                        logger.warning(f"Low stock for {prod.name}")
                except RecordValidationError as e:
                    stats["invalid"] += 1
                    logger.error(f"Invalid record line {e.line_number}: {e}")
                    if not config.processing.skip_invalid: raise

    # Atomic Export
    tmp = config.output_path.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump([p.to_dict() for p in valid_products], f, indent=2)
        tmp.replace(config.output_path)
        logger.info(f"Exported {stats['valid']} records.")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise DataExportError("Atomic write failed") from e

def main():
    # Setup demo files
    Path("data").mkdir(exist_ok=True)
    with open("data/products.csv", "w", encoding="utf-8") as f:
        f.write("product_id,name,category,price,quantity\n1,Laptop,Electronics,1000,5\n2,Apple,Food,-10,10\n3,Chair,Furniture,50,2")
    
    with open("config.yaml", "w", encoding="utf-8") as f:
        yaml.dump({
            "input": {"path": "data/products.csv"},
            "output": {"path": "output/products.json"},
            "processing": {"min_stock": 3, "allowed_categories": ["Electronics", "Furniture"], "skip_invalid": True},
            "logging": {"level": "INFO"}
        }, f)
        
    Path("output").mkdir(exist_ok=True)
    config = load_config(Path("config.yaml"))
    run_import_export(config)

if __name__ == "__main__":
    main()