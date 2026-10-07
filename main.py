import os
import sys
import csv
import json
import logging
from pathlib import Path
from typing import Generator, Dict, Any, List, Optional, Tuple
from contextlib import contextmanager
from time import perf_counter

# =====================================================================
# 1. СИСТЕМА ВЛАСНИХ ВИНЯТКІВ (DOMAIN EXCEPTIONS HIERARCHY)
# =====================================================================

class ApplicationError(Exception):
    """Базовий виняток для всієї програми."""
    pass


class DataError(ApplicationError):
    """Базовий виняток для помилок, пов'язаних з обробкою даних."""
    pass


class DataValidationError(DataError):
    """Виняток, що виникає при загальній невідповідності даних правилам валідації."""
    pass


class RecordValidationError(DataValidationError):
    """Деталізований виняток для помилки валідації конкретного запису."""

    def __init__(
        self,
        message: str,
        *,
        line_number: Optional[int] = None,
        field: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.line_number = line_number
        self.field = field

    def __str__(self) -> str:
        base_msg = super().__str__()
        details = []
        if self.line_number is not None:
            details.append(f"рядок {self.line_number}")
        if self.field is not None:
            details.append(f"поле '{self.field}'")
        if details:
            return f"{base_msg} ({', '.join(details)})"
        return base_msg


class DataImportError(DataError):
    """Помилка імпорту або читання зовнішнього ресурсу."""
    pass


class DataExportError(DataError):
    """Помилка експорту чи збереження результату."""
    pass


class ConfigurationError(ApplicationError):
    """Помилка конфігурації додатка (неправильні параметри або відсутні файли)."""
    pass


# =====================================================================
# 2. ВЛАСНІ CONTEXT MANAGERS
# =====================================================================

class AtomicFileWriter:
    """
    Контекстний менеджер для атомарного запису у файл.
    Записує дані у тимчасовий файл у тій самій директорії.
    У разі успіху файл атомарно замінює цільовий.
    У разі винятку - тимчасовий файл видаляється, не пошкоджуючи оригінал.
    """

    def __init__(self, dest_path: Path, mode: str = "w", encoding: str = "utf-8") -> None:
        self.dest_path = Path(dest_path)
        self.mode = mode
        self.encoding = encoding
        self.temp_file_path: Optional[Path] = None
        self._file = None

    def __enter__(self):
        parent = self.dest_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Створюємо тимчасовий файл в тій самій директорії (для гарантії однієї FS)
        suffix = f".tmp_{os.getpid()}"
        self.temp_file_path = parent / f"{self.dest_path.name}{suffix}"
        self._file = open(self.temp_file_path, self.mode, encoding=self.encoding)
        return self._file

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._file:
            self._file.close()

        if exc_type is not None:
            # Якщо виникла помилка, видаляємо тимчасовий файл
            if self.temp_file_path and self.temp_file_path.exists():
                try:
                    self.temp_file_path.unlink()
                except OSError:
                    pass
            return False  # Пропускаємо помилку далі

        # Якщо все успішно, виконуємо атомарну заміну
        if self.temp_file_path and self.temp_file_path.exists():
            try:
                self.temp_file_path.replace(self.dest_path)
            except OSError as err:
                raise DataExportError(f"Не вдалося атомарно перейменувати файл у {self.dest_path}") from err
        return False


@contextmanager
def execution_timer(activity_name: str) -> Generator[None, None, None]:
    """Генераторний контекстний менеджер для вимірювання часу виконання."""
    start = perf_counter()
    try:
        yield
    finally:
        elapsed = perf_counter() - start
        logging.info(f"Операція '{activity_name}' виконана за {elapsed:.6f} сек.")


# =====================================================================
# 3. ДОПОМІЖНІ ПАРСЕРИ ТА КОНФІГУРАЦІЯ
# =====================================================================

def parse_simple_yaml(content: str) -> Dict[str, Any]:
    """
    Кастомний парсер спрощеного YAML формату (key: value)
    для уникнення зовнішніх залежностей (PyYAML), якщо вони відсутні.
    """
    result = {}
    for line_no, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' not in line:
            continue
        key, val = line.split(':', 1)
        key = key.strip()
        val = val.strip()
        
        # Очищення від лапок
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
            
        # Конвертація типів даних
        if val.lower() == 'true':
            val = True
        elif val.lower() == 'false':
            val = False
        else:
            try:
                if '.' in val:
                    val = float(val)
                else:
                    val = int(val)
            except ValueError:
                pass
        result[key] = val
    return result


class AppConfig:
    """Клас конфігурації з реалізацією підходу fail-fast."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.input_file = Path(data.get("input_file", ""))
        self.input_format = str(data.get("input_format", "")).lower()
        self.output_file = Path(data.get("output_file", ""))
        self.output_format = str(data.get("output_format", "")).lower()
        self.policy = str(data.get("policy", "log_and_skip")).lower()
        self.min_temperature = float(data.get("min_temperature", -50.0))
        self.max_temperature = float(data.get("max_temperature", 100.0))
        self.min_humidity = float(data.get("min_humidity", 0.0))
        self.max_humidity = float(data.get("max_humidity", 100.0))

    def validate(self) -> None:
        """Метод fail-fast валідації конфігурації додатка."""
        if not self.input_file:
            raise ConfigurationError("Вхідний файл конфігурації не вказано.")
        if self.input_format not in ("csv", "jsonl"):
            raise ConfigurationError(f"Непідтримуваний формат входу: '{self.input_format}'. Очікується 'csv' або 'jsonl'.")
        if not self.output_file:
            raise ConfigurationError("Вихідний файл конфігурації не вказано.")
        if self.output_format not in ("csv", "jsonl"):
            raise ConfigurationError(f"Непідтримуваний формат виходу: '{self.output_format}'.")
        if self.policy not in ("strict", "log_and_skip", "force_default"):
            raise ConfigurationError(f"Невідома політика валідації: '{self.policy}'.")
        if self.min_temperature >= self.max_temperature:
            raise ConfigurationError("min_temperature має бути строго меншою за max_temperature.")
        if self.min_humidity >= self.max_humidity:
            raise ConfigurationError("min_humidity має бути строго меншою за max_humidity.")
        
        # Перевірка існування вхідного файлу
        if not self.input_file.exists():
            raise ConfigurationError(f"Вхідний файл не існує: {self.input_file.absolute()}")


# =====================================================================
# 4. СТРУКТУРИ ДАНИХ (DOMAIN MODEL)
# =====================================================================

class SensorReading:
    """Сутність телеметрії сенсора (Варіант №3)."""

    def __init__(self, sensor_id: str, timestamp: str, temperature: float, humidity: float, status: str) -> None:
        self.sensor_id = sensor_id
        self.timestamp = timestamp
        self.temperature = temperature
        self.humidity = humidity
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "timestamp": self.timestamp,
            "temperature": self.temperature,
            "humidity": self.humidity,
            "status": self.status
        }


# =====================================================================
# 5. СТРІМІНГОВЕ ЧИТАННЯ (STREAMING I/O) ТА ПАРСИНГ З EAFP
# =====================================================================

def read_csv_streaming(file_path: Path) -> Generator[Dict[str, str], None, None]:
    """Стрімінгове читання CSV-файлу рядок за рядком (Generator)."""
    with open(file_path, "r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            yield row


def read_jsonl_streaming(file_path: Path) -> Generator[Dict[str, Any], None, None]:
    """Стрімінгове читання JSON Lines файлу рядок за рядком."""
    with open(file_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line:
                yield json.loads(line)


def parse_and_validate_row(row: Dict[str, Any], line_idx: int, config: AppConfig) -> SensorReading:
    """
    Парсинг та валідація даних сенсора з використанням концепції EAFP 
    та Exception Chaining для точної діагностики помилок.
    """
    # 1. Перевірка наявності обов'язкових ключів
    for field in ("sensor_id", "timestamp", "temperature", "humidity", "status"):
        if field not in row or row[field] is None or str(row[field]).strip() == "":
            raise RecordValidationError(f"Відсутнє обов'язкове поле: '{field}'", line_number=line_idx, field=field)

    sensor_id = str(row["sensor_id"]).strip()
    timestamp = str(row["timestamp"]).strip()

    # 2. Перетворення типів з використанням EAFP
    try:
        temp_val = float(row["temperature"])
    except (ValueError, TypeError) as err:
        raise RecordValidationError(
            "Температура повинна бути числом",
            line_number=line_idx,
            field="temperature"
        ) from err
    else:
        # Цей блок виконується, якщо винятку не було. Додаткова бізнес-валідація діапазонів
        if not (config.min_temperature <= temp_val <= config.max_temperature):
            raise RecordValidationError(
                f"Температура {temp_val} виходить за межі діапазону [{config.min_temperature}, {config.max_temperature}]",
                line_number=line_idx,
                field="temperature"
            )

    try:
        hum_val = float(row["humidity"])
    except (ValueError, TypeError) as err:
        raise RecordValidationError(
            "Вологість повинна бути числом",
            line_number=line_idx,
            field="humidity"
        ) from err
    else:
        if not (config.min_humidity <= hum_val <= config.max_humidity):
            raise RecordValidationError(
                f"Вологість {hum_val} виходить за межі діапазону [{config.min_humidity}, {config.max_humidity}]",
                line_number=line_idx,
                field="humidity"
            )

    status = str(row["status"]).strip().upper()
    if status not in ("OK", "WARNING", "ERROR"):
        raise RecordValidationError(
            f"Некоректний статус: '{status}'. Очікується OK, WARNING або ERROR.",
            line_number=line_idx,
            field="status"
        )

    return SensorReading(sensor_id, timestamp, temp_val, hum_val, status)


# =====================================================================
# 6. PIPELINE ОБРОБКИ
# =====================================================================

def run_pipeline(config_path: Path) -> None:
    """Головний pipeline обробки згідно зі схемою лабораторної роботи."""
    # Відкриття ресурсу та завантаження сирої конфігурації
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            config_content = file.read()
    except OSError as err:
        raise ConfigurationError(f"Не вдалося зчитати конфігураційний файл: {config_path}") from err

    # Парсинг конфігурації (YAML або JSON)
    if config_path.suffix in (".yaml", ".yml"):
        config_data = parse_simple_yaml(config_content)
    else:
        try:
            config_data = json.loads(config_content)
        except json.JSONDecodeError as err:
            raise ConfigurationError("Некоректний формат JSON конфігурації") from err

    # Ініціалізація та Fail-fast валідація конфігурації
    config = AppConfig(config_data)
    config.validate()

    logging.info(f"Пайплайн запущено. Вхідний файл: {config.input_file} ({config.input_format.upper()})")

    # Визначення стрімінгового рідера
    if config.input_format == "csv":
        reader = read_csv_streaming(config.input_file)
    else:
        reader = read_jsonl_streaming(config.input_file)

    processed_count = 0
    written_count = 0

    # Використання власного context manager для виміру часу
    with execution_timer("Processing Pipeline"):
        try:
            # Використання власного контекстного менеджера для атомарного запису результату
            with AtomicFileWriter(config.output_file, "w") as out_file:
                writer = None
                if config.output_format == "csv":
                    writer = csv.DictWriter(
                        out_file, 
                        fieldnames=["sensor_id", "timestamp", "temperature", "humidity", "status"]
                    )
                    writer.writeheader()

                for idx, row in enumerate(reader, 1):
                    processed_count += 1
                    try:
                        reading = parse_and_validate_row(row, idx, config)
                    except RecordValidationError as error:
                        # Реалізація політики обробки помилок відповідно до конфігурації
                        if config.policy == "strict":
                            raise DataValidationError(
                                f"Критична помилка в STRICT режимі на рядку {idx}: {error}"
                            ) from error
                        
                        elif config.policy == "log_and_skip":
                            logging.warning(f"Рядок {idx} пропущено: {error}")
                            continue
                        
                        elif config.policy == "force_default":
                            logging.info(f"Рядок {idx} виправлено значеннями за замовчуванням: {error}")
                            # Застосування безпечних значень за замовчуванням
                            reading = SensorReading(
                                sensor_id=str(row.get("sensor_id") or f"UNKNOWN_{idx}"),
                                timestamp=str(row.get("timestamp") or "1970-01-01T00:00:00Z"),
                                temperature=20.0,  # default
                                humidity=50.0,     # default
                                status="WARNING"   # помічаємо як сумнівний запис
                            )
                        else:
                            raise

                    # Стрімінговий запис результату без завантаження всього масиву у пам'ять
                    if config.output_format == "csv" and writer:
                        writer.writerow(reading.to_dict())
                    else:
                        out_file.write(json.dumps(reading.to_dict(), ensure_ascii=False) + "\n")
                    
                    written_count += 1

        except Exception as pipeline_err:
            logging.error(f"Пайплайн завершився аварійно. Вихідний файл захищено від пошкоджень.")
            raise DataImportError("Помилка виконання pipeline.") from pipeline_err

    logging.info(f"Пайплайн завершено. Оброблено: {processed_count}, Записано: {written_count}")


# =====================================================================
# 7. ГЕНЕРАЦІЯ ТЕСТОВИХ ДАНИХ ТА ДЕМОНСТРАЦІЯ
# =====================================================================

def generate_demo_files() -> Tuple[Path, Path, Path, Path]:
    """Створює демо-файли для тестування різних політик у поточному каталозі."""
    demo_dir = Path("./lab5_demo_temp")
    demo_dir.mkdir(exist_ok=True)

    input_csv = demo_dir / "input_telemetry.csv"
    config_yaml = demo_dir / "config.yaml"
    output_csv = demo_dir / "output_clean.csv"
    output_jsonl = demo_dir / "output_clean.jsonl"

    # Створюємо вхідний файл телеметрії (містить правильні та завідомо некоректні рядки)
    # 1. Валідний запис
    # 2. Невалідний запис (текстове значення замість температури)
    # 3. Валідний запис
    # 4. Невалідний запис (температура поза межами валідного діапазону)
    # 5. Невалідний запис (пропущене обов'язкове поле humidity)
    # 6. Невалідний запис (некоректний статус)
    # 7. Валідний запис
    with open(input_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["sensor_id", "timestamp", "temperature", "humidity", "status"])
        writer.writerow(["SEN_A01", "2023-11-23T12:00:00Z", "24.5", "55.0", "OK"])
        writer.writerow(["SEN_A02", "2023-11-23T12:01:00Z", "NaN_Value", "60.0", "OK"])
        writer.writerow(["SEN_A03", "2023-11-23T12:02:00Z", "12.2", "45.1", "WARNING"])
        writer.writerow(["SEN_A04", "2023-11-23T12:03:00Z", "120.5", "30.0", "OK"])  # > 100.0
        writer.writerow(["SEN_A05", "2023-11-23T12:04:00Z", "25.0", "", "OK"])        # порожнє значення
        writer.writerow(["SEN_A06", "2023-11-23T12:05:00Z", "19.0", "50.0", "BAD_STATUS"])
        writer.writerow(["SEN_A07", "2023-11-23T12:06:00Z", "-5.4", "88.0", "OK"])

    # Записуємо початковий YAML конфіг для політики log_and_skip
    yaml_content = f"""# Налаштування обробки телеметрії (Варіант №3)
input_file: {input_csv.as_posix()}
input_format: csv
output_file: {output_csv.as_posix()}
output_format: csv
policy: log_and_skip
min_temperature: -50.0
max_temperature: 100.0
min_humidity: 0.0
max_humidity: 100.0
"""
    with open(config_yaml, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    return input_csv, config_yaml, output_csv, output_jsonl


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    
    print("=" * 70)
    print("СТАРТ ДЕМОНСТРАЦІЇ ЛАБОРАТОРНОЇ РОБОТИ №5 (ВАРІАНТ №3)")
    print("=" * 70)

    input_csv, config_yaml, output_csv, output_jsonl = generate_demo_files()

    print(f"\n[КРОК 1] Демо-файли згенеровано в директорії: {input_csv.parent.resolve()}")
    print(f"Вхідний файл містить 3 валідних та 4 завідомо некоректних рядки.")

    # -----------------------------------------------------------------
    # ДЕМО 1: Політика LOG_AND_SKIP
    # -----------------------------------------------------------------
    print("\n" + "-" * 50)
    print("ЕТАП 1: Запуск з політикою 'LOG_AND_SKIP' (пропуск некоректних)")
    print("-" * 50)
    
    try:
        run_pipeline(config_yaml)
        print("\n[РЕЗУЛЬТАТ] Фільтрація завершилась успішно. Вміст очищеного файлу:")
        if output_csv.exists():
            print(output_csv.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Неочікувана помилка: {e}")

    # -----------------------------------------------------------------
    # ДЕМО 2: Політика STRICT та Атомарність запису
    # -----------------------------------------------------------------
    print("\n" + "-" * 50)
    print("ЕТАП 2: Запуск з політикою 'STRICT' та тестування АТОМАРНОСТІ")
    print("-" * 50)
    
    # Модифікуємо конфіг для використання режиму strict
    with open(config_yaml, "w", encoding="utf-8") as f:
        f.write(f"""input_file: {input_csv.as_posix()}
input_format: csv
output_file: {output_jsonl.as_posix()}
output_format: jsonl
policy: strict
min_temperature: -50.0
max_temperature: 100.0
min_humidity: 0.0
max_humidity: 100.0
""")

    # Попередньо переконуємося, що цільового файлу немає
    if output_jsonl.exists():
        output_jsonl.unlink()

    print("Очікуємо падіння на першій помилці (рядок №2) через політику STRICT...")
    try:
        run_pipeline(config_yaml)
    except Exception as err:
        print(f"\n[УСПІШНО ПЕРЕХОПЛЕНО ПОМИЛКУ]: {err}")
        print(f"Першопричина винятку (Exception Chaining): {err.__cause__}")
        
        # Перевірка атомарності
        print("\nПеревірка захисту від пошкодження output-файлу:")
        print(f"Чи з'явився файл {output_jsonl.name} на диску? -> {output_jsonl.exists()} (Очікується: False)")
        if output_jsonl.exists():
            print("ПОМИЛКА: Атомарність не спрацювала! Файл було створено/залишено частково записаним.")
        else:
            print("ГАРНА РОБОТА: Атомарний менеджер запобіг збереженню неповних/пошкоджених даних на диску!")

    # -----------------------------------------------------------------
    # ДЕМО 3: Політика FORCE_DEFAULT
    # -----------------------------------------------------------------
    print("\n" + "-" * 50)
    print("ЕТАП 3: Запуск з політикою 'FORCE_DEFAULT' (автокорекція значеннями по замовчуванню)")
    print("-" * 50)
    
    with open(config_yaml, "w", encoding="utf-8") as f:
        f.write(f"""input_file: {input_csv.as_posix()}
input_format: csv
output_file: {output_jsonl.as_posix()}
output_format: jsonl
policy: force_default
min_temperature: -50.0
max_temperature: 100.0
min_humidity: 0.0
max_humidity: 100.0
""")

    try:
        run_pipeline(config_yaml)
        print("\n[РЕЗУЛЬТАТ] Обробка завершилась успішно. Вміст виправленого JSONL:")
        if output_jsonl.exists():
            print(output_jsonl.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Помилка: {e}")

    # Очищення тимчасових файлів
    print("\n" + "-" * 50)
    print("ОЧИЩЕННЯ РЕСУРСІВ ТА ТИМЧАСОВИХ ФАЙЛІВ")
    print("-" * 50)
    for path in (input_csv, config_yaml, output_csv, output_jsonl):
        if path.exists():
            path.unlink()
            print(f"Видалено тимчасовий файл: {path.name}")
    try:
        input_csv.parent.rmdir()
        print("Видалено тимчасову директорію.")
    except OSError:
        pass

    print("\n" + "=" * 70)
    print("ДЕМОНСТРАЦІЮ УСПІШНО ЗАВЕРШЕНО")
    print("=" * 70)


if __name__ == "__main__":
    main()