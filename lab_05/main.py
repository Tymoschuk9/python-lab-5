import csv
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterator, Dict, Any, Optional
from contextlib import contextmanager

# Налаштування логування
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("DataProcessor")

# --- Custom Exceptions ---
class ApplicationError(Exception):
    """Base application error."""

class DataError(ApplicationError):
    """Base data error."""

class DataValidationError(DataError):
    def __init__(self, message: str, line_number: int | None = None, field: str | None = None) -> None:
        super().__init__(message)
        self.line_number = line_number
        self.field = field

class ConfigurationError(ApplicationError):
    pass

# --- Atomic Writer ---
@contextmanager
def atomic_write(file_path: str | Path):
    """Context manager для атомарного запису файлу."""
    path = Path(file_path)
    temp_file = tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8")
    try:
        yield temp_file
        temp_file.close()
        os.replace(temp_file.name, path)
    except Exception as e:
        temp_file.close()
        os.remove(temp_file.name)
        raise e

# --- Processor ---
class DataProcessor:
    def __init__(self, config: Dict[str, Any]):
        self.config = config

    def validate_record(self, record: Dict[str, Any], line_num: int):
        if "id" not in record or not record["id"].isdigit():
            raise DataValidationError("Invalid ID format", line_number=line_num, field="id")
        if "value" not in record:
            raise DataValidationError("Missing value field", line_number=line_num, field="value")

    def process_file(self, input_path: str, output_path: str):
        if not Path(input_path).exists():
            raise ConfigurationError(f"File not found: {input_path}")

        with open(input_path, 'r', encoding='utf-8') as fin, \
             atomic_write(output_path) as fout:
            
            reader = csv.DictReader(fin)
            writer = json.dump([], fout) # Placeholder structure
            
            processed_data = []
            for i, row in enumerate(reader, start=1):
                try:
                    self.validate_record(row, i)
                    processed_data.append({"id": int(row["id"]), "value": float(row["value"])})
                except DataValidationError as e:
                    logger.error(f"Skipping line {e.line_number}: {e} (Field: {e.field})")
            
            # Write JSON output
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(processed_data, f, indent=4)

def main():
    config = {"threshold": 10}
    processor = DataProcessor(config)
    
    # Створення тестового файлу
    test_csv = "input_data.csv"
    with open(test_csv, "w", encoding="utf-8") as f:
        f.write("id,value\n1,10.5\n2,abc\n3,20.0\n")
        
    try:
        processor.process_file(test_csv, "output.json")
        print("Processing completed successfully.")
    except ApplicationError as e:
        print(f"Application error: {e}")
    finally:
        if os.path.exists(test_csv): os.remove(test_csv)

if __name__ == "__main__":
    main()