import json
import csv
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Generator, Dict, Any, List, Optional
from datetime import datetime
from contextlib import contextmanager
from time import perf_counter

# =====================================================================
# КЛАСИ ПОМИЛОК (EXCEPTION HIERARCHY)
# =====================================================================

class ApplicationError(Exception):
    """Базовий виняток для всієї програми."""
    pass


class ConfigurationError(ApplicationError):
    """Помилка конфігурації (наприклад, відсутній файл конфігурації або невірні параметри)."""
    pass


class DataError(ApplicationError):
    """Базовий виняток для помилок обробки даних."""
    pass


class DataImportError(DataError):
    """Помилка імпорту або читання файлу даних."""
    pass


class DataExportError(DataError):
    """Помилка експорту або запису файлу даних."""
    pass


class DataValidationError(DataError):
    """Базова помилка валідації структури даних."""
    pass


class RecordValidationError(DataValidationError):
    """Помилка валідації конкретного запису."""

    def __init__(
        self,
        message: str,
        *,
        line_number: Optional[int] = None,
        field: Optional[str] = None,
        raw_value: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.line_number = line_number
        self.field = field
        self.raw_value = raw_value

    def __str__(self) -> str:
        base_msg = super().__str__()
        details = []
        if self.line_number is not None:
            details.append(f"рядок/індекс: {self.line_number}")
        if self.field is not None:
            details.append(f"поле: '{self.field}'")
        if self.raw_value is not None:
            details.append(f"отримано значення: '{self.raw_value}'")
        
        if details:
            return f"{base_msg} ({', '.join(details)})"
        return base_msg


# =====================================================================
# КОНТЕКСТНІ МЕНЕДЖЕРИ (CONTEXT MANAGERS)
# =====================================================================

@contextmanager
def execution_timer(activity_name: str) -> Generator[None, None, None]:
    """Генераторний контекстний менеджер для вимірювання часу виконання."""
    start_time = perf_counter()
    logging.info(f"Початок операції: '{activity_name}'...")
    try:
        yield
    finally:
        duration = perf_counter() - start_time
        logging.info(f"Операція '{activity_name}' завершена за {duration:.6f} сек.")


class AtomicFileWriter:
    """
    Клас-контекстний менеджер для атомарного запису у файл.
    Дані спочатку записуються у тимчасовий файл у тій самій директорії,
    і лише в разі успішного завершення блоку with, тимчасовий файл
    заміщує цільовий (atomic replace). Це запобігає пошкодженню оригінального
    файлу при виникненні непередбачуваних помилок у процесі запису.
    """

    def __init__(self, target_path: Path | str, mode: str = "w", encoding: str = "utf-8") -> None:
        self.target_path = Path(target_path)
        self.mode = mode
        self.encoding = encoding
        self.temp_file = None
        self.temp_path = None

    def __enter__(self) -> Any:
        # Створюємо тимчасовий файл в тій же папці, щоб забезпечити швидку атомарну заміну
        parent_dir = self.target_path.parent
        parent_dir.mkdir(parents=True, exist_ok=True)
        
        self.temp_file = tempfile.NamedTemporaryFile(
            mode=self.mode,
            dir=parent_dir,
            delete=False,
            suffix=".tmp",
            encoding=self.encoding if "b" not in self.mode else None
        )
        self.temp_path = Path(self.temp_file.name)
        return self.temp_file

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self.temp_file:
            self.temp_file.close()

        if exc_type is None:
            # Помилок не було, робимо атомарне перейменування
            try:
                self.temp_path.replace(self.target_path)
                logging.debug(f"Атомарно записано файл: {self.target_path}")
            except Exception as e:
                if self.temp_path.exists():
                    self.temp_path.unlink()
                raise DataExportError(f"Не вдалося виконати атомарний запис у {self.target_path}") from e
        else:
            # Сталася помилка, видаляємо тимчасовий файл, оригінал залишається неушкодженим
            logging.warning("Під час запису сталася помилка. Тимчасовий файл видалено, зміни відхилено.")
            if self.temp_path and self.temp_path.exists():
                self.temp_path.unlink()
        
        return False  # Не пригнічуємо винятки, дозволяємо їм йти вище


# =====================================================================
# КЛАС КОНФІГУРАЦІЇ (CONFIGURATION MANAGER)
# =====================================================================

class AppConfig:
    """Менеджер конфігурації додатка (Fail-fast валідація при завантаженні)."""

    def __init__(self, config_path: Path | str) -> None:
        self.config_path = Path(config_path)
        self.data: Dict[str, Any] = {}
        self.load_config()

    def load_config(self) -> None:
        """Завантажує та валідує конфігураційний файл. Застосовує принцип Fail-fast."""
        if not self.config_path.exists():
            raise ConfigurationError(f"Конфігураційний файл не знайдено за шляхом: {self.config_path}")

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Помилка парсингу JSON у конфігурації: {e}") from e
        except Exception as e:
            raise ConfigurationError(f"Неочікувана помилка при читанні конфігурації: {e}") from e

        self._validate_config()

    def _validate_config(self) -> None:
        """Перевіряє наявність та коректність критичних параметрів конфігурації."""
        # Fail-fast перевірка обов'язкових секцій
        if "validation" not in self.data:
            raise ConfigurationError("Конфігурація не містить обов'язкової секції 'validation'")
        
        val_sec = self.data["validation"]
        required_keys = ["temp_min", "temp_max", "humidity_min", "humidity_max", "allowed_statuses"]
        for key in required_keys:
            if key not in val_sec:
                raise ConfigurationError(f"Відсутній обов'язковий параметр валідації: '{key}'")

        # Перевірка коректності діапазонів
        if val_sec["temp_min"] >= val_sec["temp_max"]:
            raise ConfigurationError("temp_min не може бути більшим або рівним temp_max")
        if not (0 <= val_sec["humidity_min"] < val_sec["humidity_max"] <= 100):
            raise ConfigurationError("humidity_min та humidity_max мають бути в діапазоні [0, 100]")

        # Перевірка політики обробки помилок
        policy = self.data.get("policy", "log_and_skip")
        allowed_policies = ["raise_on_invalid", "log_and_skip", "ignore_invalid"]
        if policy not in allowed_policies:
            raise ConfigurationError(f"Невідома політика обробки помилок '{policy}'. Дозволені: {allowed_policies}")

    @property
    def validation_rules(self) -> Dict[str, Any]:
        return self.data["validation"]

    @property
    def error_policy(self) -> str:
        return self.data.get("policy", "log_and_skip")

    @property
    def log_level(self) -> str:
        return self.data.get("log_level", "INFO")


# =====================================================================
# STREAMING READERS (ПРИНЦИП STREAMING I/O)
# =====================================================================

class DataStreamer:
    """Базовий потоковий зчитувач даних без повного завантаження файлу в пам'ять."""

    @staticmethod
    def stream_csv(file_path: Path) -> Generator[Dict[str, str], None, None]:
        """Потоково читає CSV файл рядок за рядком."""
        if not file_path.exists():
            raise FileNotFoundError(f"Файл не знайдено: {file_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row_idx, row in enumerate(reader, start=1):
                    # Yields row and attaches a line marker
                    row["__line_number__"] = str(row_idx)
                    yield row
        except Exception as e:
            raise DataImportError(f"Помилка при потоковому читанні CSV '{file_path}': {e}") from e

    @staticmethod
    def stream_jsonl(file_path: Path) -> Generator[Dict[str, Any], None, None]:
        """Потоково читає файл у форматі JSON Lines (JSONL) рядок за рядком."""
        if not file_path.exists():
            raise FileNotFoundError(f"Файл не знайдено: {file_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for row_idx, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict):
                            record["__line_number__"] = row_idx
                            yield record
                        else:
                            raise RecordValidationError(
                                "Запис у JSONL має бути об'єктом (dict)", 
                                line_number=row_idx
                            )
                    except json.JSONDecodeError as je:
                        raise RecordValidationError(
                            f"Некоректний JSON-синтаксис рядка: {je}", 
                            line_number=row_idx
                        ) from je
        except RecordValidationError:
            raise  # Пропускаємо далі власну помилку
        except Exception as e:
            raise DataImportError(f"Помилка читання JSONL файлу '{file_path}': {e}") from e


# =====================================================================
# ВАЛІДАТОР ТА КОНВЕРТОР ДОМЕННОЇ МОДЕЛІ (EAFP / LBYL)
# =====================================================================

class SensorReading:
    """Доменна модель показника датчика IoT (Варіант №3)."""

    def __init__(self, sensor_id: str, timestamp: datetime, temperature: float, humidity: float, status: str) -> None:
        self.sensor_id = sensor_id
        self.timestamp = timestamp
        self.temperature = temperature
        self.humidity = humidity
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        """Серіалізація доменної моделі в словник."""
        return {
            "sensor_id": self.sensor_id,
            "timestamp": self.timestamp.isoformat(),
            "temperature": round(self.temperature, 2),
            "humidity": round(self.humidity, 2),
            "status": self.status
        }

    def __repr__(self) -> str:
        return f"SensorReading({self.sensor_id}, {self.timestamp.strftime('%H:%M:%S')}, T: {self.temperature}°C, H: {self.humidity}%)"


class SensorDataValidator:
    """Валідатор сирих даних та конвертор у доменну модель."""

    def __init__(self, rules: Dict[str, Any]) -> None:
        self.rules = rules

    def validate_and_convert(self, raw_data: Dict[str, Any]) -> SensorReading:
        """
        Валідує сирі дані та повертає екземпляр SensorReading.
        Використовує підхід EAFP (Easier to Ask Forgiveness than Permission)
        для парсингу типів та виявлення відсутніх полів.
        """
        line_num = raw_data.get("__line_number__")
        if line_num is not None:
            try:
                line_num = int(line_num)
            except ValueError:
                line_num = None

        # 1. Перевірка наявності ключів та конвертація типів (EAFP)
        try:
            raw_sensor_id = raw_data["sensor_id"]
            raw_timestamp = raw_data["timestamp"]
            raw_temp = raw_data["temperature"]
            raw_hum = raw_data["humidity"]
            raw_status = raw_data["status"]
        except KeyError as ke:
            raise RecordValidationError(
                "Відсутнє обов'язкове поле у записі",
                line_number=line_num,
                field=ke.args[0]
            ) from ke

        # Конвертація ID (має бути непустим)
        sensor_id = str(raw_sensor_id).strip()
        if not sensor_id:
            raise RecordValidationError(
                "Ідентифікатор сенсора не може бути порожнім",
                line_number=line_num,
                field="sensor_id",
                raw_value=raw_sensor_id
            )

        # Конвертація часу
        try:
            # Спроба зчитати ISO-формат
            timestamp = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
        except ValueError as ve:
            raise RecordValidationError(
                "Некоректний формат дати/часу (має бути ISO 8601)",
                line_number=line_num,
                field="timestamp",
                raw_value=raw_timestamp
            ) from ve

        # Конвертація температури
        try:
            temperature = float(raw_temp)
        except ValueError as ve:
            raise RecordValidationError(
                "Температура має бути числовим значенням",
                line_number=line_num,
                field="temperature",
                raw_value=raw_temp
            ) from ve

        # Конвертація вологості
        try:
            humidity = float(raw_hum)
        except ValueError as ve:
            raise RecordValidationError(
                "Вологість має бути числовим значенням",
                line_number=line_num,
                field="humidity",
                raw_value=raw_hum
            ) from ve

        status = str(raw_status).strip().upper()

        # 2. Логічна валідація бізнес-правил (LBYL)
        if not (self.rules["temp_min"] <= temperature <= self.rules["temp_max"]):
            raise RecordValidationError(
                f"Температура поза дозволеним діапазоном [{self.rules['temp_min']}, {self.rules['temp_max']}]",
                line_number=line_num,
                field="temperature",
                raw_value=temperature
            )

        if not (self.rules["humidity_min"] <= humidity <= self.rules["humidity_max"]):
            raise RecordValidationError(
                f"Вологість поза дозволеним діапазоном [{self.rules['humidity_min']}, {self.rules['humidity_max']}]",
                line_number=line_num,
                field="humidity",
                raw_value=humidity
            )

        if status not in self.rules["allowed_statuses"]:
            raise RecordValidationError(
                f"Недопустимий статус. Дозволені: {self.rules['allowed_statuses']}",
                line_number=line_num,
                field="status",
                raw_value=status
            )

        return SensorReading(
            sensor_id=sensor_id,
            timestamp=timestamp,
            temperature=temperature,
            humidity=humidity,
            status=status
        )


# =====================================================================
# ГОЛОВНИЙ ПАЙПЛАЙН ОБРОБКИ (DATA PIPELINE)
# =====================================================================

class DataProcessingPipeline:
    """Головний керуючий клас для імпорту, валідації та експорту даних."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.validator = SensorDataValidator(config.validation_rules)

    def process(self, input_file: Path, output_file: Path) -> None:
        """Запуск повного потокового циклу обробки."""
        # Fail-fast перевірка наявності вхідного файлу
        if not input_file.exists():
            raise DataImportError(f"Вхідний файл не знайдено: {input_file}")

        suffix = input_file.suffix.lower()
        if suffix == ".csv":
            stream = DataStreamer.stream_csv(input_file)
        elif suffix in [".jsonl", ".json"]:
            # Для великих файлів використовуємо JSON Lines потоковий формат
            stream = DataStreamer.stream_jsonl(input_file)
        else:
            raise DataImportError(f"Непідтримуваний формат файлу: {suffix}")

        processed_count = 0
        skipped_count = 0

        # Використовуємо атомарний запис у вихідний файл
        # Також здійснюємо потоковий запис у вихідний JSON Lines файл
        with execution_timer(f"Обробка файлу {input_file.name} -> {output_file.name}"):
            try:
                with AtomicFileWriter(output_file, mode="w") as out_file:
                    for raw_record in stream:
                        try:
                            # Валідація та конвертація рядок за рядком
                            domain_model = self.validator.validate_and_convert(raw_record)
                            
                            # Серіалізація та запис у вихідний потік
                            serialized = json.dumps(domain_model.to_dict())
                            out_file.write(serialized + "\n")
                            processed_count += 1

                        except RecordValidationError as rve:
                            # Обробка винятків відповідно до політики з конфігурації
                            policy = self.config.error_policy
                            if policy == "raise_on_invalid":
                                logging.error(f"Критична помилка валідації запису (політика: {policy}): {rve}")
                                raise
                            elif policy == "log_and_skip":
                                logging.warning(f"Пропущено некоректний запис: {rve}")
                                skipped_count += 1
                            elif policy == "ignore_invalid":
                                # Повне ігнорування без логування
                                skipped_count += 1

            except RecordValidationError as e:
                # Повторно викидаємо для руйнування процесу, якщо встановлено політику raise_on_invalid
                raise DataError("Обробка даних перервана через некоректний запис.") from e
            except Exception as e:
                raise DataExportError(f"Помилка в процесі потокової обробки: {e}") from e

        logging.info(f"Обробка успішно завершена. Оброблено успішно: {processed_count}, пропущено: {skipped_count}")


# =====================================================================
# ДЕМОНСТРАЦІЯ РОБОТИ (MAIN)
# =====================================================================

def create_mock_environment(working_dir: Path) -> tuple[Path, Path, Path, Path]:
    """Створює необхідні тимчасові файли для демонстрації роботи системи."""
    working_dir.mkdir(parents=True, exist_ok=True)

    # 1. Створення файлу конфігурації
    config_path = working_dir / "config.json"
    config_data = {
        "validation": {
            "temp_min": -30.0,
            "temp_max": 50.0,
            "humidity_min": 10.0,
            "humidity_max": 95.0,
            "allowed_statuses": ["OK", "WARNING", "ERROR"]
        },
        "policy": "log_and_skip",  # Може бути: raise_on_invalid, log_and_skip, ignore_invalid
        "log_level": "INFO"
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=4)

    # 2. Створення вхідного файлу CSV (із декількома помилковими рядками для тестування валідації)
    csv_input_path = working_dir / "sensor_data.csv"
    csv_rows = [
        ["sensor_id", "timestamp", "temperature", "humidity", "status"],
        ["SN-001", "2023-10-27T10:00:00Z", "22.5", "45.0", "OK"],
        ["SN-002", "2023-10-27T10:05:00Z", "-45.0", "50.0", "OK"],          # Помилка: температура занизька
        ["SN-003", "2023-10-27T10:10:00Z", "18.0", "120.0", "WARNING"],     # Помилка: вологість > 100%
        ["SN-004", "Невідомий_Час", "25.0", "40.0", "ERROR"],                # Помилка: формат дати
        ["SN-005", "2023-10-27T10:20:00Z", "abc", "55.0", "OK"],            # Помилка: температура не число
        ["SN-006", "2023-10-27T10:25:00Z", "35.2", "88.5", "OK"],           # Ок запис
        ["", "2023-10-27T10:30:00Z", "12.0", "30.0", "OK"],                 # Помилка: пустий sensor_id
        ["SN-007", "2023-10-27T10:35:00Z", "15.0", "30.0", "INVALID_STAT"]   # Помилка: статус не в списку дозволених
    ]
    with open(csv_input_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(csv_rows)

    # 3. Створення JSONL файлу для демонстрації іншого формату
    jsonl_input_path = working_dir / "sensor_data.jsonl"
    jsonl_rows = [
        {"sensor_id": "SN-201", "timestamp": "2023-10-27T11:00:00Z", "temperature": 21.0, "humidity": 40.0, "status": "OK"},
        {"sensor_id": "SN-202", "timestamp": "2023-10-27T11:15:00Z", "temperature": 55.0, "humidity": 50.0, "status": "ERROR"}, # Помилка: Temp > 50
        {"sensor_id": "SN-203", "timestamp": "2023-10-27T11:30:00Z", "temperature": 15.1, "humidity": 65.2, "status": "OK"}
    ]
    with open(jsonl_input_path, "w", encoding="utf-8") as f:
        for row in jsonl_rows:
            f.write(json.dumps(row) + "\n")

    output_path = working_dir / "processed_output.jsonl"

    return config_path, csv_input_path, jsonl_input_path, output_path


def main() -> None:
    # Організація тимчасової папки для роботи
    working_dir = Path("./lab5_workspace")
    
    # Налаштування логування спочатку у базовий консольний вивід
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    try:
        logging.info("--- Підготовка мок-середовища для Лабораторної роботи №5 (Варіант №3) ---")
        config_p, csv_p, jsonl_p, out_p = create_mock_environment(working_dir)

        # 1. Завантаження конфігурації (Fail-fast)
        logging.info("\n1. Завантаження конфігурації додатка...")
        config = AppConfig(config_p)
        logging.info(f"Конфігурація успішно зчитана: {config.data}")

        # Створення пайплайну
        pipeline = DataProcessingPipeline(config)

        # 2. Обробка файлу CSV з політикою 'log_and_skip' (за замовчуванням)
        logging.info(f"\n2. Запуск обробки CSV: {csv_p.name} (Політика: log_and_skip)")
        pipeline.process(csv_p, out_p)

        # Читання результату для демонстрації
        logging.info("\nВміст вихідного файлу після обробки CSV:")
        with open(out_p, "r", encoding="utf-8") as out_f:
            for line in out_f:
                logging.info(f" Результат запису: {line.strip()}")

        # 3. Обробка файлу JSONL
        logging.info(f"\n3. Запуск обробки JSONL: {jsonl_p.name}")
        pipeline.process(jsonl_p, out_p)

        logging.info("\nВміст вихідного файлу після обробки JSONL:")
        with open(out_p, "r", encoding="utf-8") as out_f:
            for line in out_f:
                logging.info(f" Результат запису: {line.strip()}")

        # 4. Демонстрація політики 'raise_on_invalid' (Критичне падіння за вимогою бізнесу)
        logging.info("\n4. Зміна політики конфігурації на 'raise_on_invalid' (Fail-fast при обробці записів)...")
        config.data["policy"] = "raise_on_invalid"
        
        try:
            pipeline.process(csv_p, out_p)
        except DataError as de:
            logging.info(f"Перехоплено очікувану бізнес-помилку через політику 'raise_on_invalid':\n -> {de}")
            if de.__cause__:
                logging.info(f"Першопричина (Chained Exception): {de.__cause__}")

        # 5. Демонстрація атомарності запису при виникненні непередбачуваної помилки
        logging.info("\n5. Демонстрація стійкості до пошкодження вихідних файлів (Атомарний запис)...")
        
        # Визначимо завідомо некоректний вихідний файл з помилкою всередині процесу
        corrupt_out_p = working_dir / "atomic_test_output.jsonl"
        # Запишемо туди початкові валідні дані
        with open(corrupt_out_p, "w", encoding="utf-8") as test_f:
            test_f.write('{"status": "OLD_VALID_DATA"}\n')

        # Спробуємо запустити процес, який гарантовано впаде посеред обробки через політику raise_on_invalid
        try:
            pipeline.process(csv_p, corrupt_out_p)
        except DataError:
            logging.info("Помилка сталася в середині процесу, перевіримо стан файлу...")

        # Перевіримо, чи оригінальний файл залишився неушкодженим
        with open(corrupt_out_p, "r", encoding="utf-8") as test_f:
            current_content = test_f.read().strip()
            logging.info(f"Вміст файлу після аварії пайплайну: '{current_content}'")
            if "OLD_VALID_DATA" in current_content:
                logging.info("Успіх! Атомарний запис захистив старий файл від пошкоджень та перезапису.")

    except ApplicationError as ae:
        logging.critical(f"Критична системна помилка роботи додатка: {ae}")
    except Exception as e:
        logging.critical(f"Неочікуване системне відхилення: {e}")
    finally:
        # Очищення тимчасової директорії
        logging.info("\n--- Очищення тимчасових ресурсів та завершення ---")
        try:
            for file in working_dir.iterdir():
                file.unlink()
            working_dir.rmdir()
            logging.info("Робочу область успішно очищено.")
        except Exception as e:
            logging.warning(f"Не вдалося коректно очистити файли: {e}")


if __name__ == "__main__":
    main()