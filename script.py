import os
import requests
import logging
import sqlite3
import json
import base64
import uuid
import pandas as pd
import pdfplumber
import pytesseract
from PIL import Image
import io
from pathlib import Path
import re
from icalendar import Calendar, Event
from datetime import datetime, timedelta, time
import asyncio
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, ContextTypes

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация путей
DATA_DIR = Path("bot_data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot_data.db"

# Конфигурация API
TELEGRAM_TOKEN = "8573998335:AAENV4S0UhOUAmc3RpzEeFDLuModI36aqhM"
GIGACHAT_CLIENT_ID = "019ac450-7c0b-7686-a4ec-e979dd4fa0f5"
GIGACHAT_CLIENT_SECRET = "8dc579fc-56ee-49bd-b8cd-a0cd3fe4ae56"


class ReplacementParser:
    """Парсер для автоматического распознавания замен уроков из сообщений"""

    def __init__(self):
        self.days_mapping = {
            'понедельник': 'Понедельник', 'пн': 'Понедельник',
            'вторник': 'Вторник', 'вт': 'Вторник',
            'среда': 'Среда', 'ср': 'Среда',
            'четверг': 'Четверг', 'чт': 'Четверг',
            'пятница': 'Пятница', 'пт': 'Пятница',
            'суббота': 'Суббота', 'сб': 'Суббота'
        }

        self.subject_keywords = {
            'математика': ['математика', 'матеша', 'алгебра', 'геометрия', 'мат'],
            'физика': ['физика', 'физ'],
            'химия': ['химия', 'хим'],
            'биология': ['биология', 'био'],
            'история': ['история', 'ист'],
            'география': ['география', 'гео'],
            'английский': ['английский', 'англ', 'english'],
            'русский': ['русский', 'русский язык', 'яз'],
            'литература': ['литература', 'литра'],
            'информатика': ['информатика', 'инфа', 'программирование'],
            'физкультура': ['физкультура', 'физра', 'спорт'],
            'обществознание': ['обществознание', 'общество'],
            'технология': ['технология', 'труд'],
            'музыка': ['музыка', 'пение'],
            'изо': ['изо', 'рисование']
        }

    def parse_replacement_message(self, message: str) -> dict:
        """Парсинг сообщения о заменах и извлечение структурированной информации"""
        message_lower = message.lower()

        # Убираем лишние слова (обращения и т.д.)
        cleaned_message = self._clean_message(message_lower)

        # Извлекаем день
        day = self._extract_day(cleaned_message)

        # Извлекаем номер урока
        lesson_number = self._extract_lesson_number(cleaned_message)

        # Извлекаем замену (старый и новый предмет)
        replacement = self._extract_replacement(cleaned_message)

        # Извлекаем кабинет
        classroom = self._extract_classroom(cleaned_message)

        result = {
            'day': day,
            'lesson_number': lesson_number,
            'old_subject': replacement.get('old_subject'),
            'new_subject': replacement.get('new_subject'),
            'classroom': classroom,
            'is_cancellation': replacement.get('is_cancellation', False),
            'success': bool(day and (replacement.get('old_subject') or replacement.get('new_subject')))
        }

        logger.info(f"🔍 Распознана замена: {result}")
        return result

    def _clean_message(self, message: str) -> str:
        """Очистка сообщения от лишних слов"""
        # Убираем обращения и общие фразы
        stop_phrases = [
            'ребята', 'ученики', 'дорогие', 'уважаемые', 'сообщаю', 'информирую',
            'обратите внимание', 'довожу до вашего сведения', 'замена'
        ]

        cleaned = message
        for phrase in stop_phrases:
            cleaned = cleaned.replace(phrase, '')

        return cleaned.strip()

    def _extract_day(self, message: str) -> str:
        """Извлечение дня недели из сообщения"""
        # Сначала проверяем относительные дни
        if 'завтра' in message:
            return self._get_day_by_offset(1)
        elif 'послезавтра' in message:
            return self._get_day_by_offset(2)
        elif 'сегодня' in message:
            return self._get_day_by_offset(0)

        # Затем ищем конкретные дни недели
        for keyword, day in self.days_mapping.items():
            if keyword in message:
                return day

        return None

    def _extract_lesson_number(self, message: str) -> int:
        """Извлечение номера урока"""
        # Паттерны для номеров уроков
        patterns = [
            r'(\d+)[-ыи]?м?\s+урок',
            r'урок\s+(\d+)',
            r'(\d+)[-ыи]?й?\s+урок',
            r'на\s+(\d+)[-ыи]?м?\s+уроке'
        ]

        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue

        return None

    def _extract_replacement(self, message: str) -> dict:
        """Извлечение информации о замене предмета"""
        result = {'is_cancellation': False}

        # Паттерн для замены: "вместо X будет Y"
        replacement_pattern = r'вместо\s+([^\s,]+(?:\s+[^\s,]+)*)\s+будет\s+([^\s,]+(?:\s+[^\s,]+)*)'
        match = re.search(replacement_pattern, message)

        if match:
            old_subject = self._normalize_subject(match.group(1))
            new_subject = self._normalize_subject(match.group(2))

            if old_subject and new_subject:
                result['old_subject'] = old_subject
                result['new_subject'] = new_subject
                return result

        # Паттерн для отмены: "не будет", "отменяется"
        cancellation_patterns = [
            r'не будет\s+([^\s,]+(?:\s+[^\s,]+)*)',
            r'отменяется\s+([^\s,]+(?:\s+[^\s,]+)*)',
            r'отмена\s+([^\s,]+(?:\s+[^\s,]+)*)'
        ]

        for pattern in cancellation_patterns:
            match = re.search(pattern, message)
            if match:
                subject = self._normalize_subject(match.group(1))
                if subject:
                    result['old_subject'] = subject
                    result['is_cancellation'] = True
                    return result

        # Если не нашли структурированный паттерн, пытаемся извлечь предметы по ключевым словам
        subjects = self._find_subjects_in_text(message)
        if len(subjects) >= 2:
            result['old_subject'] = subjects[0]
            result['new_subject'] = subjects[1]
        elif len(subjects) == 1:
            result['old_subject'] = subjects[0]
            result['is_cancellation'] = True

        return result

    def _extract_classroom(self, message: str) -> str:
        """Извлечение номера кабинета"""
        classroom_patterns = [
            r'в?\s*кабинете?\s*(\d+)',
            r'каб\.?\s*(\d+)',
            r'аудитори[ия]\s*(\d+)',
            r'(\d+)[-ыи]?й?\s*кабинет'
        ]

        for pattern in classroom_patterns:
            match = re.search(pattern, message)
            if match:
                return match.group(1)

        return None

    def _normalize_subject(self, subject_text: str) -> str:
        """Нормализация названия предмета"""
        subject_lower = subject_text.lower().strip()

        for subject, keywords in self.subject_keywords.items():
            for keyword in keywords:
                if keyword in subject_lower:
                    return subject

        return subject_text  # Возвращаем как есть, если не нашли в словаре

    def _find_subjects_in_text(self, message: str) -> list:
        """Поиск предметов в тексте по ключевым словам"""
        found_subjects = []

        for subject, keywords in self.subject_keywords.items():
            for keyword in keywords:
                if keyword in message and subject not in found_subjects:
                    found_subjects.append(subject)

        return found_subjects

    def _get_day_by_offset(self, offset: int) -> str:
        """Получение дня недели по смещению от сегодняшнего дня"""
        days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        today = datetime.now().weekday()
        target_day = (today + offset) % 7
        return days[target_day]


class ScheduleEditor:
    """Класс для редактирования расписания - добавления и удаления уроков"""

    def __init__(self, db_path):
        self.db_path = db_path

    def check_lesson_slot(self, user_id: int, day: str, lesson_number: int) -> dict:
        """Проверка, занят ли слот для урока"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                '''SELECT subject, room, teacher FROM schedule 
                WHERE user_id = ? AND day = ? AND lesson_number = ?''',
                (user_id, day, lesson_number)
            )

            existing_lesson = cursor.fetchone()
            conn.close()

            if existing_lesson:
                return {
                    'occupied': True,
                    'subject': existing_lesson[0],
                    'room': existing_lesson[1],
                    'teacher': existing_lesson[2]
                }
            else:
                return {'occupied': False}

        except Exception as e:
            logger.error(f"❌ Ошибка проверки слота урока: {e}")
            return {'occupied': False, 'error': str(e)}

    def add_lesson(self, user_id: int, day: str, lesson_number: int, subject: str,
                   room: str = "", teacher: str = "", start_time: str = "") -> dict:
        """Добавление урока в расписание"""
        try:
            # Проверяем, не занят ли слот
            slot_check = self.check_lesson_slot(user_id, day, lesson_number)

            if slot_check['occupied']:
                return {
                    'success': False,
                    'message': f"❌ Слот уже занят! В это время стоит: {slot_check['subject']}",
                    'occupied': True,
                    'existing_lesson': {
                        'subject': slot_check['subject'],
                        'room': slot_check['room'],
                        'teacher': slot_check['teacher']
                    }
                }

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                '''INSERT INTO schedule 
                (user_id, day, lesson_number, start_time, subject, room, teacher) 
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (user_id, day, lesson_number, start_time, subject, room, teacher)
            )

            conn.commit()
            conn.close()

            logger.info(f"✅ Добавлен урок: {day}, №{lesson_number}, {subject}")
            return {
                'success': True,
                'message': f"✅ Урок добавлен: {day}, {lesson_number}-й урок - {subject}"
            }

        except Exception as e:
            logger.error(f"❌ Ошибка добавления урока: {e}")
            return {
                'success': False,
                'message': f"❌ Ошибка при добавлении урока: {str(e)}"
            }

    def replace_lesson(self, user_id: int, day: str, lesson_number: int, subject: str,
                       room: str = "", teacher: str = "", start_time: str = "") -> dict:
        """Замена существующего урока"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Получаем информацию о старом уроке
            cursor.execute(
                '''SELECT subject, room, teacher FROM schedule 
                WHERE user_id = ? AND day = ? AND lesson_number = ?''',
                (user_id, day, lesson_number)
            )
            old_lesson = cursor.fetchone()

            # Обновляем урок
            cursor.execute(
                '''UPDATE schedule SET subject = ?, room = ?, teacher = ?, start_time = ?
                WHERE user_id = ? AND day = ? AND lesson_number = ?''',
                (subject, room, teacher, start_time, user_id, day, lesson_number)
            )

            conn.commit()
            conn.close()

            old_subject = old_lesson[0] if old_lesson else "неизвестный предмет"

            logger.info(f"🔄 Заменен урок: {day}, №{lesson_number}, {old_subject} → {subject}")
            return {
                'success': True,
                'message': f"🔄 Урок заменен: {old_subject} → {subject}"
            }

        except Exception as e:
            logger.error(f"❌ Ошибка замены урока: {e}")
            return {
                'success': False,
                'message': f"❌ Ошибка при замене урока: {str(e)}"
            }

    def remove_lesson(self, user_id: int, day: str, lesson_number: int = None, subject: str = None) -> dict:
        """Удаление урока из расписания"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            if lesson_number is not None:
                # Удаляем по номеру урока
                cursor.execute(
                    '''SELECT subject FROM schedule 
                    WHERE user_id = ? AND day = ? AND lesson_number = ?''',
                    (user_id, day, lesson_number)
                )
                lesson_to_remove = cursor.fetchone()

                if not lesson_to_remove:
                    conn.close()
                    return {
                        'success': False,
                        'message': f"❌ Урок №{lesson_number} не найден в расписании на {day}"
                    }

                cursor.execute(
                    'DELETE FROM schedule WHERE user_id = ? AND day = ? AND lesson_number = ?',
                    (user_id, day, lesson_number)
                )
                removed_subject = lesson_to_remove[0]

            elif subject is not None:
                # Удаляем по предмету
                cursor.execute(
                    'DELETE FROM schedule WHERE user_id = ? AND day = ? AND subject LIKE ?',
                    (user_id, day, f'%{subject}%')
                )
                removed_count = cursor.rowcount
                removed_subject = subject

                if removed_count == 0:
                    conn.close()
                    return {
                        'success': False,
                        'message': f"❌ Предмет '{subject}' не найден в расписании на {day}"
                    }
            else:
                conn.close()
                return {
                    'success': False,
                    'message': "❌ Укажите номер урока или предмет для удаления"
                }

            conn.commit()
            conn.close()

            logger.info(f"🗑️ Удален урок: {day}, {removed_subject}")
            return {
                'success': True,
                'message': f"🗑️ Урок удален: {day} - {removed_subject}"
            }

        except Exception as e:
            logger.error(f"❌ Ошибка удаления урока: {e}")
            return {
                'success': False,
                'message': f"❌ Ошибка при удалении урока: {str(e)}"
            }

    def parse_add_command(self, message: str) -> dict:
        """Парсинг команды добавления урока"""
        message_lower = message.lower()

        # Извлекаем день
        day = self._extract_day(message_lower)
        if not day:
            return {'success': False, 'message': "❌ Не удалось определить день недели"}

        # Извлекаем номер урока
        lesson_number = self._extract_lesson_number(message_lower)
        if not lesson_number:
            return {'success': False, 'message': "❌ Не удалось определить номер урока"}

        # Извлекаем предмет
        subject = self._extract_subject(message_lower)
        if not subject:
            return {'success': False, 'message': "❌ Не удалось определить предмет"}

        # Извлекаем кабинет
        classroom = self._extract_classroom(message_lower)

        return {
            'success': True,
            'day': day,
            'lesson_number': lesson_number,
            'subject': subject,
            'room': classroom,
            'teacher': ""  # Можно расширить для извлечения учителя
        }

    def parse_remove_command(self, message: str) -> dict:
        """Парсинг команды удаления урока"""
        message_lower = message.lower()

        # Извлекаем день
        day = self._extract_day(message_lower)
        if not day:
            return {'success': False, 'message': "❌ Не удалось определить день недели"}

        # Пытаемся извлечь номер урока
        lesson_number = self._extract_lesson_number(message_lower)

        # Если нет номера, пытаемся извлечь предмет
        subject = None
        if not lesson_number:
            subject = self._extract_subject(message_lower)
            if not subject:
                return {'success': False, 'message': "❌ Не удалось определить номер урока или предмет"}

        return {
            'success': True,
            'day': day,
            'lesson_number': lesson_number,
            'subject': subject
        }

    def _extract_day(self, message: str) -> str:
        """Извлечение дня недели"""
        days_mapping = {
            'понедельник': 'Понедельник', 'пн': 'Понедельник',
            'вторник': 'Вторник', 'вт': 'Вторник',
            'среда': 'Среда', 'ср': 'Среда',
            'четверг': 'Четверг', 'чт': 'Четверг',
            'пятница': 'Пятница', 'пт': 'Пятница',
            'суббота': 'Суббота', 'сб': 'Суббота'
        }

        for keyword, day in days_mapping.items():
            if keyword in message:
                return day

        # Проверяем относительные дни
        if 'завтра' in message:
            return self._get_day_by_offset(1)
        elif 'послезавтра' in message:
            return self._get_day_by_offset(2)
        elif 'сегодня' in message:
            return self._get_day_by_offset(0)

        return None

    def _extract_lesson_number(self, message: str) -> int:
        """Извлечение номера урока"""
        patterns = [
            r'(\d+)[-ыи]?м?\s+урок',
            r'урок\s+(\d+)',
            r'(\d+)[-ыи]?й?\s+урок',
            r'на\s+(\d+)[-ыи]?м?\s+уроке'
        ]

        for pattern in patterns:
            match = re.search(pattern, message)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue

        return None

    def _extract_subject(self, message: str) -> str:
        """Извлечение предмета"""
        subject_keywords = {
            'математика': ['математика', 'матеша', 'алгебра', 'геометрия', 'мат'],
            'физика': ['физика', 'физ'],
            'химия': ['химия', 'хим'],
            'биология': ['биология', 'био'],
            'история': ['история', 'ист'],
            'география': ['география', 'гео'],
            'английский': ['английский', 'англ', 'english'],
            'русский': ['русский', 'русский язык', 'яз'],
            'литература': ['литература', 'литра'],
            'информатика': ['информатика', 'инфа', 'программирование'],
            'физкультура': ['физкультура', 'физра', 'спорт']
        }

        for subject, keywords in subject_keywords.items():
            for keyword in keywords:
                if keyword in message:
                    return subject

        return None

    def _extract_classroom(self, message: str) -> str:
        """Извлечение номера кабинета"""
        classroom_patterns = [
            r'в?\s*кабинете?\s*(\d+)',
            r'каб\.?\s*(\d+)',
            r'аудитори[ия]\s*(\d+)',
            r'(\d+)[-ыи]?й?\s*кабинет'
        ]

        for pattern in classroom_patterns:
            match = re.search(pattern, message)
            if match:
                return match.group(1)

        return ""

    def _get_day_by_offset(self, offset: int) -> str:
        """Получение дня недели по смещению"""
        days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        today = datetime.now().weekday()
        target_day = (today + offset) % 7
        return days[target_day]


class ScheduleRAGSystem:
    """RAG система для интеллектуальной работы с расписанием"""

    def __init__(self):
        self.days_mapping = {
            'понедельник': 'Понедельник',
            'вторник': 'Вторник',
            'среда': 'Среда',
            'четверг': 'Четверг',
            'пятница': 'Пятница',
            'суббота': 'Суббота',
            'воскресенье': 'Воскресенье'
        }

        self.day_keywords = {
            'сегодня': 0,
            'завтра': 1,
            'послезавтра': 2,
            'понедельник': 0,
            'вторник': 1,
            'среду': 2,
            'четверг': 3,
            'пятницу': 4,
            'субботу': 5
        }

        self.lesson_keywords = {
            'первый': 1, '1': 1, '1-ый': 1, '1-й': 1,
            'второй': 2, '2': 2, '2-ой': 2, '2-й': 2,
            'третий': 3, '3': 3, '3-ий': 3, '3-й': 3,
            'четвертый': 4, '4': 4, '4-ый': 4, '4-й': 4,
            'пятый': 5, '5': 5, '5-ый': 5, '5-й': 5,
            'шестой': 6, '6': 6, '6-ой': 6, '6-й': 6,
            'седьмой': 7, '7': 7, '7-ой': 7, '7-й': 7,
            'восьмой': 8, '8': 8, '8-ой': 8, '8-й': 8
        }

        self.subject_keywords = {
            'математика': ['математика', 'матеша', 'алгебра', 'геометрия'],
            'русский': ['русский', 'русский язык', 'яз', 'литература'],
            'физика': ['физика', 'физ'],
            'химия': ['химия', 'хим'],
            'биология': ['биология', 'био'],
            'история': ['история', 'ист'],
            'география': ['география', 'гео'],
            'английский': ['английский', 'англ', 'english'],
            'информатика': ['информатика', 'инфа', 'программирование'],
            'физра': ['физра', 'физкультура', 'спорт'],
            'обж': ['обж', 'безопасность'],
            'музыка': ['музыка', 'пение'],
            'рисование': ['рисование', 'изо', 'изобразительное']
        }

    def parse_question(self, question: str) -> dict:
        """Парсинг вопроса пользователя и извлечение сущностей"""
        question_lower = question.lower()

        # Определяем тип запроса
        intent = self._detect_intent(question_lower)

        # Извлекаем сущности
        entities = {
            'day': self._extract_day(question_lower),
            'lesson_number': self._extract_lesson_number(question_lower),
            'subject': self._extract_subject(question_lower),
            'intent': intent
        }

        logger.info(f"🎯 Распознан интент: {intent}, сущности: {entities}")
        return entities

    def _detect_intent(self, question: str) -> str:
        """Определение намерения пользователя"""
        if any(word in question for word in ['какой', 'первый', 'второй', 'урок']):
            return 'lesson_query'
        elif any(word in question for word in ['когда', 'во сколько', 'время']):
            return 'time_query'
        elif any(word in question for word in ['где', 'кабинет', 'аудитория']):
            return 'room_query'
        elif any(word in question for word in ['учитель', 'преподаватель']):
            return 'teacher_query'
        elif any(word in question for word in ['окно', 'свободно', 'перерыв']):
            return 'gap_query'
        elif any(word in question for word in ['сколько', 'уроков']):
            return 'count_query'
        else:
            return 'general_query'

    def _extract_day(self, question: str) -> str:
        """Извлечение дня из вопроса"""
        for keyword, day in self.days_mapping.items():
            if keyword in question:
                return day

        # Обработка относительных дней
        if 'сегодня' in question:
            return self._get_day_by_offset(0)
        elif 'завтра' in question:
            return self._get_day_by_offset(1)
        elif 'послезавтра' in question:
            return self._get_day_by_offset(2)

        return None

    def _extract_lesson_number(self, question: str) -> int:
        """Извлечение номера урока"""
        for keyword, number in self.lesson_keywords.items():
            if keyword in question:
                return number
        return None

    def _extract_subject(self, question: str) -> str:
        """Извлечение предмета"""
        for subject, keywords in self.subject_keywords.items():
            for keyword in keywords:
                if keyword in question:
                    return subject
        return None

    def _get_day_by_offset(self, offset: int) -> str:
        """Получение дня недели по смещению"""
        days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
        today = datetime.now().weekday()
        target_day = (today + offset) % 7
        return days[target_day]

    def find_gaps(self, lessons: list) -> list:
        """Поиск окон в расписании"""
        if not lessons:
            return []

        gaps = []
        lessons_sorted = sorted(lessons, key=lambda x: x[1])  # Сортируем по номеру урока

        # Проверяем пропуски между уроками
        for i in range(len(lessons_sorted) - 1):
            current_lesson = lessons_sorted[i]
            next_lesson = lessons_sorted[i + 1]

            if next_lesson[1] - current_lesson[1] > 1:
                gap_start = current_lesson[1] + 1
                gap_end = next_lesson[1] - 1
                gaps.append((gap_start, gap_end))

        return gaps

    def generate_precise_answer(self, entities: dict, lessons: list, day: str) -> str:
        """Генерация точного ответа на основе данных из расписания"""
        if not lessons:
            return "❌ В расписании нет информации на этот день."

        intent = entities['intent']
        subject = entities['subject']
        lesson_number = entities['lesson_number']

        if intent == 'lesson_query':
            return self._answer_lesson_query(lessons, lesson_number, day)
        elif intent == 'time_query':
            return self._answer_time_query(lessons, subject, lesson_number, day)
        elif intent == 'room_query':
            return self._answer_room_query(lessons, subject, day)
        elif intent == 'teacher_query':
            return self._answer_teacher_query(lessons, subject, day)
        elif intent == 'gap_query':
            return self._answer_gap_query(lessons, day)
        elif intent == 'count_query':
            return self._answer_count_query(lessons, day)
        else:
            return self._answer_general_query(lessons, day)

    def _answer_lesson_query(self, lessons: list, lesson_number: int, day: str) -> str:
        """Ответ на вопрос о конкретном уроке"""
        if lesson_number:
            for lesson in lessons:
                if lesson[1] == lesson_number:  # lesson_number
                    time_display = f"🕒 {lesson[2]}" if lesson[2] else f"урок №{lesson[1]}"
                    room_display = f" в кабинете {lesson[4]}" if lesson[4] else ""
                    teacher_display = f" ({lesson[5]})" if lesson[5] else ""
                    return f"📚 {day}, {lesson_number}-ый урок: {lesson[3]}{room_display}{teacher_display}\n{time_display}"
            return f"❌ {lesson_number}-го урока нет в расписании на {day}"
        else:
            # Показываем все уроки дня
            response = f"📅 Расписание на {day}:\n\n"
            for lesson in sorted(lessons, key=lambda x: x[1]):
                time_display = f"🕒 {lesson[2]}" if lesson[2] else f"{lesson[1]}."
                room_display = f" 🚪 {lesson[4]}" if lesson[4] else ""
                teacher_display = f" 👨‍🏫 {lesson[5]}" if lesson[5] else ""
                response += f"{time_display} {lesson[3]}{room_display}{teacher_display}\n"
            return response

    def _answer_time_query(self, lessons: list, subject: str, lesson_number: int, day: str) -> str:
        """Ответ на вопрос о времени"""
        if subject:
            subject_lessons = [lesson for lesson in lessons if subject in lesson[3].lower()]
            if subject_lessons:
                response = f"⏰ {subject.title()} в расписании на {day}:\n"
                for lesson in subject_lessons:
                    time_display = f"🕒 {lesson[2]}" if lesson[2] else f"{lesson[1]}-ый урок"
                    response += f"• {time_display}\n"
                return response
            else:
                return f"❌ {subject.title()} нет в расписании на {day}"
        elif lesson_number:
            return self._answer_lesson_query(lessons, lesson_number, day)
        else:
            return "❌ Уточните, по какому предмету или уроку вы хотите узнать время."

    def _answer_room_query(self, lessons: list, subject: str, day: str) -> str:
        """Ответ на вопрос о кабинете"""
        if subject:
            subject_lessons = [lesson for lesson in lessons if subject in lesson[3].lower()]
            if subject_lessons:
                response = f"🚪 {subject.title()} на {day}:\n"
                for lesson in subject_lessons:
                    room_info = f"кабинет {lesson[4]}" if lesson[4] else "кабинет не указан"
                    time_info = f" ({lesson[2]})" if lesson[2] else f" ({lesson[1]}-ый урок)"
                    response += f"• {room_info}{time_info}\n"
                return response
            else:
                return f"❌ {subject.title()} нет в расписании на {day}"
        else:
            return "❌ Уточните, по какому предмету вы хотите узнать кабинет."

    def _answer_teacher_query(self, lessons: list, subject: str, day: str) -> str:
        """Ответ на вопрос об учителе"""
        if subject:
            subject_lessons = [lesson for lesson in lessons if subject in lesson[3].lower()]
            teachers = set(lesson[5] for lesson in subject_lessons if lesson[5])

            if teachers:
                teachers_list = ", ".join(teachers)
                return f"👨‍🏫 {subject.title()} на {day} преподает: {teachers_list}"
            else:
                return f"❌ В расписании на {day} не указан учитель для {subject}"
        else:
            return "❌ Уточните, по какому предмету вы хотите узнать учителя."

    def _answer_gap_query(self, lessons: list, day: str) -> str:
        """Ответ на вопрос об окнах"""
        gaps = self.find_gaps(lessons)

        if not gaps:
            return f"📅 На {day} нет окон между уроками"

        response = f"🪟 Окна в расписании на {day}:\n"
        for gap_start, gap_end in gaps:
            if gap_start == gap_end:
                response += f"• {gap_start}-ый урок\n"
            else:
                response += f"• С {gap_start}-го по {gap_end}-ый урок\n"

        return response

    def _answer_count_query(self, lessons: list, day: str) -> str:
        """Ответ на вопрос о количестве уроков"""
        count = len(lessons)
        return f"📊 На {day} {count} уроков"

    def _answer_general_query(self, lessons: list, day: str) -> str:
        """Общий ответ с расписанием"""
        return self._answer_lesson_query(lessons, None, day)


class CalendarExporter:
    """Класс для экспорта расписания в календарь"""

    def __init__(self):
        # Стандартное время уроков (если не указано в расписании)
        self.default_lesson_times = {
            1: ("08:00", "08:45"),
            2: ("09:00", "09:45"),
            3: ("10:00", "10:45"),
            4: ("11:00", "11:45"),
            5: ("12:00", "12:45"),
            6: ("13:00", "13:45"),
            7: ("14:00", "14:45"),
            8: ("15:00", "15:45"),
        }

        # Маппинг дней недели
        self.days_mapping = {
            'Понедельник': 0,
            'Вторник': 1,
            'Среда': 2,
            'Четверг': 3,
            'Пятница': 4,
            'Суббота': 5,
            'Воскресенье': 6
        }

    def get_next_week_dates(self):
        """Получить даты на следующую неделю"""
        today = datetime.now()
        start_of_week = today - timedelta(days=today.weekday())
        next_week_start = start_of_week + timedelta(days=7)

        dates = {}
        for day_name, day_offset in self.days_mapping.items():
            date = next_week_start + timedelta(days=day_offset)
            dates[day_name] = date
        return dates

    def parse_time(self, time_str):
        """Парсинг времени из строки"""
        try:
            if ':' in time_str:
                hours, minutes = map(int, time_str.split(':'))
                return hours, minutes
            return None, None
        except:
            return None, None

    def create_calendar_event(self, lesson, date, event_number):
        """Создание события для календаря"""
        event = Event()
        event.add('uid', f"{uuid.uuid4()}@school-bot")
        event.add('dtstamp', datetime.now())

        # Название события
        summary = f"{lesson['subject']}"
        if 'контрольная' in lesson['subject'].lower():
            summary = f"📝 {summary}"
        elif 'лабораторная' in lesson['subject'].lower():
            summary = f"🔬 {summary}"
        else:
            summary = f"📚 {summary}"

        event.add('summary', summary)

        # Определяем время начала и окончания
        start_time, end_time = self.get_lesson_time(lesson)
        start_datetime = datetime.combine(date, start_time)
        end_datetime = datetime.combine(date, end_time)

        event.add('dtstart', start_datetime)
        event.add('dtend', end_datetime)

        # Описание
        description_parts = []
        if lesson.get('teacher'):
            description_parts.append(f"Учитель: {lesson['teacher']}")
        if lesson.get('room'):
            description_parts.append(f"Кабинет: {lesson['room']}")
        description_parts.append(f"Урок №{lesson['lesson_number']}")

        event.add('description', '\n'.join(description_parts))

        # Местоположение
        if lesson.get('room'):
            event.add('location', f"Кабинет {lesson['room']}")

        # Напоминание (за 15 минут до начала)
        alarm = Event()
        alarm.add('action', 'DISPLAY')
        alarm.add('description', f'Скоро урок: {lesson["subject"]}')
        alarm.add('trigger', timedelta(minutes=-15))
        event.add_component(alarm)

        # Дополнительное напоминание (за 5 минут)
        alarm_5min = Event()
        alarm_5min.add('action', 'DISPLAY')
        alarm_5min.add('description', f'Через 5 минут: {lesson["subject"]}')
        alarm_5min.add('trigger', timedelta(minutes=-5))
        event.add_component(alarm_5min)

        return event

    def get_lesson_time(self, lesson):
        """Получить время начала и окончания урока"""
        # Если время указано в расписании
        if lesson.get('start_time'):
            time_parts = lesson['start_time'].split('-')
            if len(time_parts) == 2:
                start_str, end_str = time_parts
                start_hours, start_minutes = self.parse_time(start_str.strip())
                end_hours, end_minutes = self.parse_time(end_str.strip())

                if start_hours is not None and end_hours is not None:
                    return (datetime.min.replace(hour=start_hours, minute=start_minutes).time(),
                            datetime.min.replace(hour=end_hours, minute=end_minutes).time())

        # Используем стандартное время по номеру урока
        lesson_num = lesson.get('lesson_number', 1)
        if lesson_num in self.default_lesson_times:
            start_str, end_str = self.default_lesson_times[lesson_num]
            start_hours, start_minutes = self.parse_time(start_str)
            end_hours, end_minutes = self.parse_time(end_str)

            return (datetime.min.replace(hour=start_hours, minute=start_minutes).time(),
                    datetime.min.replace(hour=end_hours, minute=end_minutes).time())

        # По умолчанию
        return (datetime.min.replace(hour=8, minute=0).time(),
                datetime.min.replace(hour=8, minute=45).time())

    def generate_ics_file(self, lessons, weeks=1):
        """Генерация .ics файла с расписанием"""
        cal = Calendar()
        cal.add('prodid', '-//School Schedule Bot//RU')
        cal.add('version', '2.0')
        cal.add('name', 'Расписание уроков')
        cal.add('x-wr-calname', 'Расписание уроков')

        # Генерируем события на несколько недель вперед
        for week in range(weeks):
            week_dates = self.get_next_week_dates()
            week_offset = timedelta(weeks=week)

            for lesson in lessons:
                day_name = lesson['day']
                if day_name in week_dates:
                    date = week_dates[day_name] + week_offset
                    event = self.create_calendar_event(lesson, date, lesson['lesson_number'])
                    cal.add_component(event)

        return cal.to_ical()

    def generate_daily_reminders(self, lessons, days=7):
        """Генерация ежедневных напоминаний о расписании"""
        cal = Calendar()
        cal.add('prodid', '-//School Schedule Reminders//RU')
        cal.add('version', '2.0')
        cal.add('name', 'Напоминания о расписании')
        cal.add('x-wr-calname', 'Напоминания о расписании')

        today = datetime.now().date()

        for day in range(days):
            current_date = today + timedelta(days=day)
            day_name_russian = list(self.days_mapping.keys())[current_date.weekday()]

            # Находим уроки на этот день
            day_lessons = [lesson for lesson in lessons if lesson['day'] == day_name_russian]

            if day_lessons:
                # Создаем событие-напоминание на утро
                reminder_event = Event()
                reminder_event.add('uid', f"{uuid.uuid4()}@school-bot-reminder")
                reminder_event.add('dtstamp', datetime.now())

                # Сортируем уроки по времени
                day_lessons_sorted = sorted(day_lessons, key=lambda x: x['lesson_number'])

                # Формируем описание
                schedule_text = "📅 Сегодня:\n"
                for lesson in day_lessons_sorted:
                    start_time, end_time = self.get_lesson_time(lesson)
                    schedule_text += f"• {start_time.strftime('%H:%M')} - {lesson['subject']}"
                    if lesson.get('room'):
                        schedule_text += f" ({lesson['room']})"
                    schedule_text += "\n"

                reminder_event.add('summary', '📚 Расписание на сегодня')
                reminder_event.add('description', schedule_text)

                # Напоминание в 7:00 утра
                reminder_time = datetime.combine(current_date, datetime.min.replace(hour=7, minute=0).time())
                reminder_event.add('dtstart', reminder_time)
                reminder_event.add('dtend', reminder_time + timedelta(minutes=15))

                # Алерт за 0 минут (сразу)
                alarm = Event()
                alarm.add('action', 'DISPLAY')
                alarm.add('description', 'Посмотри расписание на сегодня')
                alarm.add('trigger', timedelta(minutes=0))
                reminder_event.add_component(alarm)

                cal.add_component(reminder_event)

        return cal.to_ical()


class DayComplexityAnalyzer:
    """Анализатор сложности учебного дня"""

    def __init__(self):
        # Веса различных типов занятий
        self.weights = {
            'обычный_урок': 1,
            'контрольная': 2,
            'лабораторная': 1.5,
            'экзамен': 3,
            'зачет': 1.5
        }

        # Сложность предметов (можно расширить)
        self.subject_difficulty = {
            'математика': 1.2,
            'физика': 1.3,
            'химия': 1.2,
            'русский': 1.1,
            'литература': 1.0,
            'история': 1.0,
            'биология': 1.1,
            'география': 1.0,
            'английский': 1.1,
            'информатика': 1.2,
            'алгебра': 1.3,
            'геометрия': 1.3,
            'обществознание': 1.0
        }

    def detect_lesson_type(self, subject: str, teacher: str = "") -> str:
        """Определение типа занятия по названию предмета"""
        subject_lower = subject.lower()

        if any(word in subject_lower for word in ['контрольная', 'к/р', 'тест', 'проверочная']):
            return 'контрольная'
        elif any(word in subject_lower for word in ['лабораторная', 'лаб', 'практикум']):
            return 'лабораторная'
        elif any(word in subject_lower for word in ['экзамен', 'зачет']):
            return 'экзамен' if 'экзамен' in subject_lower else 'зачет'
        else:
            return 'обычный_урок'

    def calculate_day_complexity(self, lessons: list) -> dict:
        """Расчет сложности дня на основе расписания"""
        if not lessons:
            return {'score': 0, 'level': 'пустой', 'recommendations': []}

        total_score = 0
        lesson_count = len(lessons)
        test_count = 0
        difficult_subjects = []

        for lesson in lessons:
            subject = lesson.get('subject', '')
            teacher = lesson.get('teacher', '')

            # Определяем тип занятия
            lesson_type = self.detect_lesson_type(subject, teacher)

            # Базовый вес занятия
            base_weight = self.weights.get(lesson_type, 1)

            # Учитываем сложность предмета
            subject_base = subject.lower().split()[0] if subject else ''
            difficulty_multiplier = 1.0
            for subj, multiplier in self.subject_difficulty.items():
                if subj in subject.lower():
                    difficulty_multiplier = multiplier
                    break

            # Итоговый балл за занятие
            lesson_score = base_weight * difficulty_multiplier
            total_score += lesson_score

            # Считаем контрольные
            if lesson_type == 'контрольная':
                test_count += 1

            # Отмечаем сложные предметы
            if difficulty_multiplier >= 1.2:
                difficult_subjects.append(subject)

        # Нормализуем оценку (максимум 10 баллов)
        # Базовый расчет: учитываем количество уроков и контрольных
        base_score = min(10, lesson_count * 0.8 + test_count * 1.5)

        # Корректируем на основе сложности предметов
        difficulty_bonus = len(difficult_subjects) * 0.5
        normalized_score = min(10, round(base_score + difficulty_bonus, 1))

        # Определяем уровень сложности
        if normalized_score <= 3:
            level = 'легкий'
        elif normalized_score <= 6:
            level = 'средний'
        elif normalized_score <= 8:
            level = 'сложный'
        else:
            level = 'очень сложный'

        # Формируем рекомендации
        recommendations = self._generate_recommendations(
            normalized_score, lesson_count, test_count, difficult_subjects
        )

        return {
            'score': normalized_score,
            'level': level,
            'lesson_count': lesson_count,
            'test_count': test_count,
            'difficult_subjects': difficult_subjects,
            'recommendations': recommendations
        }

    def _generate_recommendations(self, score: float, lesson_count: int,
                                  test_count: int, difficult_subjects: list) -> list:
        """Генерация рекомендаций на основе анализа дня"""
        recommendations = []

        if score >= 8:
            recommendations.extend([
                "🔥 Это будет напряженный день!",
                "📚 Начни готовиться к урокам заранее, сегодня вечером",
                "⏰ Ложись спать пораньше, чтобы выспаться",
                "🍎 Не забудь про полноценный завтрак",
                "💧 Бери с собой бутылку воды"
            ])
        elif score >= 6:
            recommendations.extend([
                "📖 День потребует сосредоточенности",
                "🕔 Сегодня до 19:00 сделай основную домашку",
                "🎵 Вечером выдели время для отдыха",
                "📋 Составь план подготовки на вечер"
            ])
        elif score >= 4:
            recommendations.extend([
                "📝 День средней нагрузки",
                "🕠 Можешь делать домашку до 20:00",
                "🚶 Не забывай про прогулки на свежем воздухе"
            ])
        else:
            recommendations.extend([
                "😊 Легкий день - отличная возможность!",
                "📚 Закончи домашние задания быстро",
                "🎯 Займись чем-то полезным для себя",
                "👥 Проведи время с друзьями или семьей"
            ])

        # Дополнительные рекомендации по количеству контрольных
        if test_count >= 2:
            recommendations.append("✏️ Целых 2 контрольные! Повтори материалы сегодня вечером")
        elif test_count == 1:
            recommendations.append("📝 Завтра контрольная - удели ей особое внимание")

        # Рекомендации по сложным предметам
        if difficult_subjects:
            subjects_str = ", ".join(difficult_subjects[:2])
            recommendations.append(f"🎯 Сложные предметы: {subjects_str} - повтори их первыми")

        return recommendations


class ScheduleParser:
    """Класс для парсинга расписания из разных форматов"""

    @staticmethod
    def parse_excel(file_content: bytes):
        """Парсинг Excel файла с расписанием"""
        try:
            logger.info("🔍 Начинаю парсинг Excel файла...")

            # Читаем Excel файл
            df = pd.read_excel(io.BytesIO(file_content))
            logger.info(f"📊 Загружен DataFrame с {len(df)} строками и {len(df.columns)} колонками")
            logger.info(f"📋 Колонки: {list(df.columns)}")

            lessons = []

            # Преобразуем DataFrame в список уроков
            for index, row in df.iterrows():
                try:
                    # Более гибкое извлечение данных
                    day = str(row.get('День', row.get('день', ''))).strip()
                    lesson_number = int(row.get('Номер_урока', row.get('номер_урока', 0)))
                    start_time = str(row.get('Время', row.get('время', ''))).strip()
                    subject = str(row.get('Предмет', row.get('предмет', ''))).strip()
                    room = str(row.get('Кабинет', row.get('кабинет', ''))).strip()
                    teacher = str(row.get('Учитель', row.get('учитель', ''))).strip()

                    # Проверяем, что есть основные данные
                    if day and subject:
                        lesson = {
                            'day': day,
                            'lesson_number': lesson_number,
                            'start_time': start_time,
                            'subject': subject,
                            'room': room,
                            'teacher': teacher
                        }
                        lessons.append(lesson)
                        logger.info(f"✅ Добавлен урок: {lesson}")
                    else:
                        logger.warning(f"⚠️ Пропущена строка {index}: недостаточно данных")

                except Exception as e:
                    logger.error(f"❌ Ошибка в строке {index}: {e}")
                    continue

            logger.info(f"📚 Всего распознано уроков: {len(lessons)}")
            return lessons

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга Excel: {e}")
            return []

    @staticmethod
    def parse_pdf(file_content: bytes):
        """Парсинг PDF файла с расписанием"""
        try:
            logger.info("🔍 Начинаю парсинг PDF файла...")
            lessons = []

            with pdfplumber.open(io.BytesIO(file_content)) as pdf:
                logger.info(f"📄 PDF содержит {len(pdf.pages)} страниц")

                for page_num, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        logger.info(f"📖 Страница {page_num + 1}: {len(text)} символов")

                        # Простой парсинг текста PDF
                        lines = text.split('\n')
                        current_day = None

                        for line_num, line in enumerate(lines):
                            line = line.strip()
                            if not line:
                                continue

                            # Определяем день недели
                            day_keywords = {
                                'понедельник': 'Понедельник',
                                'вторник': 'Вторник',
                                'среда': 'Среда',
                                'четверг': 'Четверг',
                                'пятница': 'Пятница',
                                'суббота': 'Суббота'
                            }

                            for keyword, day in day_keywords.items():
                                if keyword in line.lower():
                                    current_day = day
                                    logger.info(f"📅 Найден день: {current_day}")
                                    break

                            # Парсим строку с уроком
                            if current_day and any(char.isdigit() for char in line):
                                # Упрощенный парсинг - ищем номер урока и предмет
                                parts = re.split(r'\s+', line)

                                lesson_number = None
                                subject_parts = []

                                for part in parts:
                                    # Ищем номер урока
                                    if part.replace('.', '').isdigit() and not lesson_number:
                                        try:
                                            lesson_number = int(part.replace('.', ''))
                                            continue
                                        except:
                                            pass

                                    # Собираем части предмета
                                    if part and not part.isdigit():
                                        subject_parts.append(part)

                                if lesson_number and subject_parts:
                                    subject = ' '.join(subject_parts)
                                    lesson = {
                                        'day': current_day,
                                        'lesson_number': lesson_number,
                                        'start_time': '',
                                        'subject': subject,
                                        'room': '',
                                        'teacher': ''
                                    }
                                    lessons.append(lesson)
                                    logger.info(f"✅ Добавлен урок из PDF: {lesson}")

            logger.info(f"📚 Всего распознано уроков из PDF: {len(lessons)}")
            return lessons

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга PDF: {e}")
            return []

    @staticmethod
    def parse_image(file_content: bytes):
        """Парсинг изображения с расписанием с помощью OCR"""
        try:
            logger.info("🔍 Начинаю распознавание изображения...")

            # Открываем изображение
            image = Image.open(io.BytesIO(file_content))
            logger.info(f"🖼️ Размер изображения: {image.size}")

            # Улучшаем качество для лучшего распознавания
            image = image.convert('L')  # Grayscale

            # Распознаем текст
            custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя .,-'
            text = pytesseract.image_to_string(image, lang='rus+eng', config=custom_config)

            logger.info(f"📖 Распознанный текст: {text[:500]}...")  # Логируем первые 500 символов

            # Парсим распознанный текст
            lessons = []
            lines = text.split('\n')
            current_day = None

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Определяем день недели
                day_keywords = {
                    'понедельник': 'Понедельник',
                    'вторник': 'Вторник',
                    'среда': 'Среда',
                    'четверг': 'Четверг',
                    'пятница': 'Пятница',
                    'суббота': 'Суббота'
                }

                for keyword, day in day_keywords.items():
                    if keyword in line.lower():
                        current_day = day
                        logger.info(f"📅 Найден день на изображении: {current_day}")
                        break

                # Парсим строку с уроком
                if current_day and any(char.isdigit() for char in line):
                    # Упрощенный парсинг для изображений
                    parts = re.split(r'\s+', line)

                    lesson_number = None
                    subject_parts = []

                    for part in parts:
                        # Ищем номер урока (только цифры)
                        if part.replace('.', '').isdigit() and not lesson_number:
                            try:
                                lesson_number = int(part.replace('.', ''))
                                if 1 <= lesson_number <= 8:  # Проверяем, что это валидный номер урока
                                    continue
                                else:
                                    lesson_number = None  # Сбрасываем если номер невалидный
                            except:
                                pass

                        # Собираем части предмета (не цифры и не слишком короткие)
                        if part and not part.isdigit() and len(part) > 2:
                            subject_parts.append(part)

                    if lesson_number and subject_parts:
                        subject = ' '.join(subject_parts)
                        lesson = {
                            'day': current_day,
                            'lesson_number': lesson_number,
                            'start_time': '',
                            'subject': subject,
                            'room': '',
                            'teacher': ''
                        }
                        lessons.append(lesson)
                        logger.info(f"✅ Добавлен урок из изображения: {lesson}")

            logger.info(f"📚 Всего распознано уроков из изображения: {len(lessons)}")
            return lessons

        except Exception as e:
            logger.error(f"❌ Ошибка парсинга изображения: {e}")
            return []


class GigaChatService:
    def __init__(self):
        self.access_token = None
        self.token_expires = None

    def get_access_token(self):
        """Получение access token для GigaChat API"""
        try:
            credentials = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()

            url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Authorization': f'Basic {encoded_credentials}',
                'Accept': 'application/json',
                'RqUID': str(uuid.uuid4())
            }
            data = {'scope': 'GIGACHAT_API_PERS'}

            response = requests.post(url, headers=headers, data=data, verify=False)

            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data.get('access_token')
                logger.info("✅ GigaChat token получен успешно")
                return self.access_token
            else:
                logger.error(f"❌ Ошибка получения token: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"❌ Ошибка при получении token: {e}")
            return None

    def send_message(self, text):
        """Отправка сообщения в GigaChat"""
        try:
            if not self.access_token:
                if not self.get_access_token():
                    return None

            url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }

            data = {
                "model": "GigaChat",
                "messages": [
                    {
                        "role": "system",
                        "content": "Ты - полезный помощник для школьников и студентов. Отвечай кратко, понятно и по делу. Помогай с учебой, объяснением тем, домашними заданиями и организацией времени."
                    },
                    {
                        "role": "user",
                        "content": text
                    }
                ],
                "temperature": 0.7,
                "max_tokens": 1000
            }

            response = requests.post(url, headers=headers, json=data, verify=False)

            if response.status_code == 200:
                result = response.json()
                return result['choices'][0]['message']['content']
            else:
                logger.error(f"❌ Ошибка API: {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к GigaChat: {e}")
            return None


class TelegramBot:
    def __init__(self):
        self.gigachat = GigaChatService()
        self.parser = ScheduleParser()
        self.analyzer = DayComplexityAnalyzer()
        self.calendar_exporter = CalendarExporter()
        self.rag_system = ScheduleRAGSystem()
        self.replacement_parser = ReplacementParser()
        self.schedule_editor = ScheduleEditor(DB_PATH)  # Новый редактор расписания
        self.init_db()

    def init_db(self):
        """Инициализация базы данных SQLite"""
        try:
            logger.info(f"🔄 Инициализация БД по пути: {DB_PATH}")
            logger.info(f"📁 Директория существует: {DATA_DIR.exists()}")

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Таблица для диалогов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    message TEXT,
                    response TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица для расписания
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT,
                    lesson_number INTEGER,
                    start_time TEXT,
                    subject TEXT,
                    room TEXT,
                    teacher TEXT,
                    user_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица для загруженных файлов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS uploaded_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    file_name TEXT,
                    file_type TEXT,
                    file_size INTEGER,
                    upload_date DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица для настроек уведомлений
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    morning_reminder BOOLEAN DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Новая таблица для истории замен
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS replacement_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    day TEXT,
                    lesson_number INTEGER,
                    old_subject TEXT,
                    new_subject TEXT,
                    classroom TEXT,
                    is_cancellation BOOLEAN,
                    replacement_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                    original_message TEXT
                )
            ''')

            conn.commit()

            # Проверяем создание таблиц
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            logger.info(f"✅ Созданы таблицы: {[table[0] for table in tables]}")

            conn.close()
            logger.info("✅ База данных успешно инициализирована")

        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")

    def save_conversation(self, user_id, message, response):
        """Сохранение диалога в базу данных"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                'INSERT INTO conversations (user_id, message, response) VALUES (?, ?, ?)',
                (user_id, message, response)
            )

            conn.commit()
            conn.close()
            logger.info(f"💾 Сохранен диалог для пользователя {user_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения диалога: {e}")

    def save_schedule(self, user_id, lessons):
        """Сохранение расписания в базу данных"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Удаляем старое расписание пользователя
            cursor.execute('DELETE FROM schedule WHERE user_id = ?', (user_id,))

            # Сохраняем новое расписание
            for lesson in lessons:
                cursor.execute(
                    '''INSERT INTO schedule 
                    (day, lesson_number, start_time, subject, room, teacher, user_id) 
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (lesson['day'], lesson.get('lesson_number', 0),
                     lesson.get('start_time', ''), lesson['subject'],
                     lesson.get('room', ''), lesson.get('teacher', ''), user_id)
                )

            # Включаем утренние уведомления по умолчанию при загрузке расписания
            cursor.execute(
                'INSERT OR REPLACE INTO notifications (user_id, morning_reminder) VALUES (?, 1)',
                (user_id,)
            )

            conn.commit()
            conn.close()
            logger.info(f"💾 Сохранено {len(lessons)} уроков для пользователя {user_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения расписания: {e}")
            return False

    def get_schedule(self, user_id, day=None):
        """Получение расписания из базы данных"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            if day:
                cursor.execute(
                    '''SELECT day, lesson_number, start_time, subject, room, teacher 
                    FROM schedule WHERE user_id = ? AND day = ? ORDER BY lesson_number''',
                    (user_id, day)
                )
            else:
                cursor.execute(
                    '''SELECT day, lesson_number, start_time, subject, room, teacher 
                    FROM schedule WHERE user_id = ? ORDER BY day, lesson_number''',
                    (user_id,)
                )

            lessons = cursor.fetchall()
            conn.close()
            logger.info(f"📖 Загружено {len(lessons)} уроков для пользователя {user_id}")
            return lessons
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки расписания: {e}")
            return []

    def apply_replacement(self, user_id, replacement_data, original_message):
        """Применение замены к расписанию"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            day = replacement_data['day']
            lesson_number = replacement_data['lesson_number']
            old_subject = replacement_data['old_subject']
            new_subject = replacement_data['new_subject']
            classroom = replacement_data['classroom']
            is_cancellation = replacement_data['is_cancellation']

            # Сохраняем в историю замен
            cursor.execute(
                '''INSERT INTO replacement_history 
                (user_id, day, lesson_number, old_subject, new_subject, classroom, is_cancellation, original_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (user_id, day, lesson_number, old_subject, new_subject, classroom, is_cancellation, original_message)
            )

            # Получаем текущее расписание на этот день для отладки
            cursor.execute(
                'SELECT lesson_number, subject, room FROM schedule WHERE user_id = ? AND day = ? ORDER BY lesson_number',
                (user_id, day)
            )
            current_schedule = cursor.fetchall()
            logger.info(f"📋 Текущее расписание на {day}: {current_schedule}")

            affected_rows = 0

            if is_cancellation:
                # Отмена урока
                if lesson_number and old_subject:
                    # Пытаемся найти урок по номеру и предмету
                    cursor.execute(
                        'DELETE FROM schedule WHERE user_id = ? AND day = ? AND lesson_number = ? AND subject LIKE ?',
                        (user_id, day, lesson_number, f'%{old_subject}%')
                    )
                    affected_rows = cursor.rowcount

                    if affected_rows == 0:
                        # Если не нашли по точному совпадению, пробуем найти только по номеру урока
                        cursor.execute(
                            'DELETE FROM schedule WHERE user_id = ? AND day = ? AND lesson_number = ?',
                            (user_id, day, lesson_number)
                        )
                        affected_rows = cursor.rowcount
                elif old_subject:
                    # Отмена по предмету (без номера урока)
                    cursor.execute(
                        'DELETE FROM schedule WHERE user_id = ? AND day = ? AND subject LIKE ?',
                        (user_id, day, f'%{old_subject}%')
                    )
                    affected_rows = cursor.rowcount
            else:
                # Замена предмета
                if lesson_number and old_subject:
                    # Обновляем конкретный урок
                    if new_subject:
                        # Сначала пытаемся найти урок по номеру и старому предмету
                        cursor.execute(
                            'UPDATE schedule SET subject = ? WHERE user_id = ? AND day = ? AND lesson_number = ? AND subject LIKE ?',
                            (new_subject, user_id, day, lesson_number, f'%{old_subject}%')
                        )
                        affected_rows = cursor.rowcount

                        if affected_rows == 0:
                            # Если не нашли, пробуем обновить только по номеру урока
                            cursor.execute(
                                'UPDATE schedule SET subject = ? WHERE user_id = ? AND day = ? AND lesson_number = ?',
                                (new_subject, user_id, day, lesson_number)
                            )
                            affected_rows = cursor.rowcount

                        # Если указан кабинет, обновляем и его
                        if classroom and affected_rows > 0:
                            cursor.execute(
                                'UPDATE schedule SET room = ? WHERE user_id = ? AND day = ? AND lesson_number = ?',
                                (classroom, user_id, day, lesson_number)
                            )
                elif lesson_number and new_subject:
                    # Если есть только номер урока и новый предмет (добавление урока)
                    cursor.execute(
                        'INSERT INTO schedule (user_id, day, lesson_number, subject, room) VALUES (?, ?, ?, ?, ?)',
                        (user_id, day, lesson_number, new_subject, classroom or '')
                    )
                    affected_rows = 1

            conn.commit()
            conn.close()

            logger.info(
                f"🔄 Применена замена для пользователя {user_id}: {replacement_data}, затронуто строк: {affected_rows}")
            return affected_rows > 0

        except Exception as e:
            logger.error(f"❌ Ошибка применения замены: {e}")
            return False

    def save_uploaded_file(self, user_id, file_name, file_type, file_size):
        """Сохранение информации о загруженном файле"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                'INSERT INTO uploaded_files (user_id, file_name, file_type, file_size) VALUES (?, ?, ?, ?)',
                (user_id, file_name, file_type, file_size)
            )

            conn.commit()
            conn.close()
            logger.info(f"💾 Сохранена информация о файле {file_name}")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения информации о файле: {e}")

    def get_users_with_morning_reminders(self):
        """Получение списка пользователей с включенными утренними уведомлениями"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute(
                'SELECT user_id FROM notifications WHERE morning_reminder = 1'
            )

            users = [row[0] for row in cursor.fetchall()]
            conn.close()
            return users
        except Exception as e:
            logger.error(f"❌ Ошибка получения пользователей с уведомлениями: {e}")
            return []

    async def handle_replacement_message(self, update: Update, context: CallbackContext, message: str):
        """Обработка сообщения о заменах уроков"""
        user = update.effective_user

        await update.message.reply_text("🔍 Анализирую сообщение о заменах...")

        # Парсим сообщение о заменах
        replacement_data = self.replacement_parser.parse_replacement_message(message)

        if not replacement_data['success']:
            await update.message.reply_text(
                "❌ Не удалось распознать информацию о заменах в сообщении.\n\n"
                "💡 Пример правильного формата:\n"
                "\"Ребята, завтра 5-м уроком вместо физики будет история в 302 кабинете\"\n\n"
                "📝 **Распознанные данные:**\n"
                f"День: {replacement_data.get('day', 'не распознан')}\n"
                f"Номер урока: {replacement_data.get('lesson_number', 'не распознан')}\n"
                f"Старый предмет: {replacement_data.get('old_subject', 'не распознан')}\n"
                f"Новый предмет: {replacement_data.get('new_subject', 'не распознан')}\n"
                f"Отмена: {'да' if replacement_data.get('is_cancellation') else 'нет'}"
            )
            return

        # Проверяем, есть ли расписание у пользователя
        user_schedule = self.get_schedule(user.id)
        if not user_schedule:
            await update.message.reply_text(
                "❌ Не удалось применить замену: расписание не загружено.\n\n"
                "Сначала загрузите расписание через кнопку «📤 Загрузить расписание»"
            )
            return

        # Применяем замену к расписанию
        success = self.apply_replacement(user.id, replacement_data, message)

        if success:
            # Формируем информативное сообщение о примененной замене
            response = "✅ **Замена применена!**\n\n"

            if replacement_data['is_cancellation']:
                response += f"📅 {replacement_data['day']}\n"
                if replacement_data['lesson_number']:
                    response += f"❌ Отменен {replacement_data['lesson_number']}-й урок: {replacement_data['old_subject']}\n"
                else:
                    response += f"❌ Отменен урок: {replacement_data['old_subject']}\n"
            else:
                response += f"📅 {replacement_data['day']}\n"
                if replacement_data['lesson_number']:
                    response += f"🔄 {replacement_data['lesson_number']}-й урок: {replacement_data['old_subject']} → {replacement_data['new_subject']}\n"
                else:
                    response += f"🔄 Замена: {replacement_data['old_subject']} → {replacement_data['new_subject']}\n"

                if replacement_data['classroom']:
                    response += f"🚪 Кабинет: {replacement_data['classroom']}\n"

            response += "\n📝 Расписание автоматически обновлено!"

            # Показываем обновленное расписание на этот день
            day_schedule = self.get_schedule(user.id, replacement_data['day'])
            if day_schedule:
                response += f"\n\n📅 **Обновленное расписание на {replacement_data['day']}:**\n"
                for lesson in sorted(day_schedule, key=lambda x: x[1]):
                    time_display = f"🕒 {lesson[2]}" if lesson[2] else f"{lesson[1]}."
                    room_display = f" 🚪 {lesson[4]}" if lesson[4] else ""
                    teacher_display = f" 👨‍🏫 {lesson[5]}" if lesson[5] else ""
                    response += f"{time_display} {lesson[3]}{room_display}{teacher_display}\n"
        else:
            # Детальная диагностика проблемы
            day_schedule = self.get_schedule(user.id, replacement_data['day'])

            response = (
                "❌ Не удалось применить замену к расписанию.\n\n"
                "**Возможные причины:**\n"
            )

            if not day_schedule:
                response += "• 📅 На указанный день нет расписания\n"
            elif replacement_data['lesson_number']:
                # Проверяем, есть ли урок с таким номером
                lesson_exists = any(lesson[1] == replacement_data['lesson_number'] for lesson in day_schedule)
                if not lesson_exists:
                    response += f"• 🔢 В расписании нет {replacement_data['lesson_number']}-го урока\n"

                # Проверяем, есть ли старый предмет
                if replacement_data['old_subject']:
                    subject_exists = any(
                        replacement_data['old_subject'] in lesson[3].lower()
                        for lesson in day_schedule
                        if lesson[1] == replacement_data['lesson_number']
                    )
                    if not subject_exists:
                        response += f"• 📚 На {replacement_data['lesson_number']}-м уроке нет предмета '{replacement_data['old_subject']}'\n"

            response += "\n**💡 Что проверить:**\n"
            response += "• Правильно ли распознан день и номер урока\n"
            response += "• Совпадают ли названия предметов с расписанием\n"
            response += "• Загружено ли актуальное расписание\n\n"
            response += "**📅 Текущее расписание на этот день:**\n"

            if day_schedule:
                for lesson in sorted(day_schedule, key=lambda x: x[1]):
                    time_display = f"🕒 {lesson[2]}" if lesson[2] else f"{lesson[1]}."
                    room_display = f" 🚪 {lesson[4]}" if lesson[4] else ""
                    response += f"{time_display} {lesson[3]}{room_display}\n"
            else:
                response += "Расписание на этот день не найдено\n"

            response += "\nИсправьте сообщение о замене и попробуйте снова!"

        await update.message.reply_text(response)

    def is_replacement_message(self, message: str) -> bool:
        """Проверка, является ли сообщение уведомлением о заменах"""
        message_lower = message.lower()

        # Ключевые слова, указывающие на сообщение о заменах
        replacement_keywords = [
            'вместо', 'будет', 'замена', 'отменяется', 'не будет',
            'переносится', 'изменения', 'уроком', 'завтра', 'сегодня'
        ]

        subject_keywords = [
            'математика', 'физика', 'химия', 'биология', 'история',
            'география', 'английский', 'русский', 'литература', 'информатика'
        ]

        # Сообщение считается уведомлением о заменах, если содержит ключевые слова И названия предметов
        has_replacement_words = any(word in message_lower for word in replacement_keywords)
        has_subject_words = any(word in message_lower for word in subject_keywords)

        return has_replacement_words and has_subject_words

    async def handle_add_lesson(self, update: Update, context: CallbackContext, message: str):
        """Обработка команды добавления урока"""
        user = update.effective_user

        await update.message.reply_text("🔍 Анализирую команду добавления урока...")

        # Парсим команду
        parsed_data = self.schedule_editor.parse_add_command(message)

        if not parsed_data['success']:
            await update.message.reply_text(parsed_data['message'])
            return

        # Проверяем, занят ли слот
        day = parsed_data['day']
        lesson_number = parsed_data['lesson_number']
        subject = parsed_data['subject']
        room = parsed_data['room']

        # Добавляем урок
        result = self.schedule_editor.add_lesson(
            user.id, day, lesson_number, subject, room
        )

        if result['success']:
            response = f"✅ {result['message']}\n\n"

            # Показываем обновленное расписание на этот день
            day_schedule = self.get_schedule(user.id, day)
            if day_schedule:
                response += f"📅 **Расписание на {day}:**\n"
                for lesson in sorted(day_schedule, key=lambda x: x[1]):
                    time_display = f"🕒 {lesson[2]}" if lesson[2] else f"{lesson[1]}."
                    room_display = f" 🚪 {lesson[4]}" if lesson[4] else ""
                    teacher_display = f" 👨‍🏫 {lesson[5]}" if lesson[5] else ""
                    response += f"{time_display} {lesson[3]}{room_display}{teacher_display}\n"
        else:
            if result.get('occupied'):
                # Слот занят - предлагаем замену
                existing_lesson = result['existing_lesson']
                response = (
                    f"⚠️ {result['message']}\n\n"
                    f"📅 {day}, {lesson_number}-й урок уже занят:\n"
                    f"• {existing_lesson['subject']}"
                    f"{f' в кабинете {existing_lesson['room']}' if existing_lesson['room'] else ''}"
                    f"{f' ({existing_lesson['teacher']})' if existing_lesson['teacher'] else ''}\n\n"
                    "Хотите заменить этот урок?\n\n"
                    "✅ **Да** - заменить существующий урок\n"
                    "❌ **Нет** - оставить как есть\n\n"
                    "Отправьте 'да' или 'нет'"
                )

                # Сохраняем данные для подтверждения замены
                context.user_data['pending_replacement'] = {
                    'day': day,
                    'lesson_number': lesson_number,
                    'subject': subject,
                    'room': room,
                    'existing_subject': existing_lesson['subject']
                }
            else:
                response = f"❌ {result['message']}"

        await update.message.reply_text(response)

    async def handle_remove_lesson(self, update: Update, context: CallbackContext, message: str):
        """Обработка команды удаления урока"""
        user = update.effective_user

        await update.message.reply_text("🔍 Анализирую команду удаления урока...")

        # Парсим команду
        parsed_data = self.schedule_editor.parse_remove_command(message)

        if not parsed_data['success']:
            await update.message.reply_text(parsed_data['message'])
            return

        day = parsed_data['day']
        lesson_number = parsed_data['lesson_number']
        subject = parsed_data['subject']

        # Удаляем урок
        result = self.schedule_editor.remove_lesson(user.id, day, lesson_number, subject)

        if result['success']:
            response = f"✅ {result['message']}\n\n"

            # Показываем обновленное расписание на этот день
            day_schedule = self.get_schedule(user.id, day)
            if day_schedule:
                response += f"📅 **Обновленное расписание на {day}:**\n"
                for lesson in sorted(day_schedule, key=lambda x: x[1]):
                    time_display = f"🕒 {lesson[2]}" if lesson[2] else f"{lesson[1]}."
                    room_display = f" 🚪 {lesson[4]}" if lesson[4] else ""
                    teacher_display = f" 👨‍🏫 {lesson[5]}" if lesson[5] else ""
                    response += f"{time_display} {lesson[3]}{room_display}{teacher_display}\n"
            else:
                response += f"📅 На {day} больше нет уроков"
        else:
            response = f"❌ {result['message']}"

        await update.message.reply_text(response)

    async def handle_replace_confirmation(self, update: Update, context: CallbackContext, message: str):
        """Обработка подтверждения замены урока"""
        user = update.effective_user
        user_message = message.lower().strip()

        if 'pending_replacement' not in context.user_data:
            await update.message.reply_text("❌ Нет ожидающих подтверждения операций")
            return

        replacement_data = context.user_data['pending_replacement']

        if user_message in ['да', 'yes', 'ок', 'подтверждаю']:
            # Подтверждаем замену
            result = self.schedule_editor.replace_lesson(
                user.id,
                replacement_data['day'],
                replacement_data['lesson_number'],
                replacement_data['subject'],
                replacement_data['room']
            )

            if result['success']:
                response = f"✅ {result['message']}\n\n"

                # Показываем обновленное расписание
                day_schedule = self.get_schedule(user.id, replacement_data['day'])
                if day_schedule:
                    response += f"📅 **Обновленное расписание на {replacement_data['day']}:**\n"
                    for lesson in sorted(day_schedule, key=lambda x: x[1]):
                        time_display = f"🕒 {lesson[2]}" if lesson[2] else f"{lesson[1]}."
                        room_display = f" 🚪 {lesson[4]}" if lesson[4] else ""
                        teacher_display = f" 👨‍🏫 {lesson[5]}" if lesson[5] else ""
                        response += f"{time_display} {lesson[3]}{room_display}{teacher_display}\n"
            else:
                response = f"❌ {result['message']}"

            # Очищаем данные подтверждения
            del context.user_data['pending_replacement']

        elif user_message in ['нет', 'no', 'отмена']:
            response = "❌ Замена отменена. Существующий урок сохранен."
            # Очищаем данные подтверждения
            del context.user_data['pending_replacement']
        else:
            response = "❌ Не понял ваш ответ. Отправьте 'да' для подтверждения замены или 'нет' для отмены."

        await update.message.reply_text(response)

    def is_add_lesson_command(self, message: str) -> bool:
        """Проверка, является ли сообщение командой добавления урока"""
        message_lower = message.lower()
        add_keywords = ['добавь', 'добавить', 'внеси', 'запиши', 'новый урок']
        lesson_keywords = ['урок']

        has_add_words = any(word in message_lower for word in add_keywords)
        has_lesson_words = any(word in message_lower for word in lesson_keywords)

        return has_add_words and has_lesson_words

    def is_remove_lesson_command(self, message: str) -> bool:
        """Проверка, является ли сообщение командой удаления урока"""
        message_lower = message.lower()
        remove_keywords = ['удали', 'удалить', 'отмени', 'убери', 'убери урок']
        lesson_keywords = ['урок']

        has_remove_words = any(word in message_lower for word in remove_keywords)
        has_lesson_words = any(word in message_lower for word in lesson_keywords)

        return has_remove_words and has_lesson_words

    async def start(self, update: Update, context: CallbackContext):
        """Обработчик команды /start"""
        user = update.effective_user

        # Проверяем состояние базы данных
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table';")
            table_count = cursor.fetchone()[0]
            conn.close()
            db_status = f"✅ База данных: {table_count} таблиц"
        except Exception as e:
            db_status = f"❌ База данных: ошибка ({e})"

        keyboard = [
            [KeyboardButton("📚 Помощь с учебой"), KeyboardButton("🤖 Задать вопрос")],
            [KeyboardButton("📅 Моё расписание"), KeyboardButton("📤 Загрузить расписание")],
            [KeyboardButton("📋 Скачать шаблон"), KeyboardButton("📊 Оценить завтра")],
            [KeyboardButton("➕ Добавить урок"), KeyboardButton("➖ Удалить урок")],
            [KeyboardButton("📅 Экспорт в календарь"), KeyboardButton("📈 Статистика")],
            [KeyboardButton("ℹ️ О боте")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await update.message.reply_text(
            f"Привет, {user.first_name}! 👋\n\n"
            f"{db_status}\n\n"
            "Я твой умный помощник с интеграцией GigaChat! 🤖\n\n"
            "🎯 **Что я умею:**\n"
            "• Отвечать на вопросы по учебе\n"
            "• Помогать с домашними заданиями\n"
            "• Объяснять сложные темы\n"
            "• Читать расписание из файлов\n"
            "• Хранить твое расписание\n"
            "• **Автоматически обрабатывать замены уроков** 🔄\n"
            "• **Добавлять и удалять уроки** ➕➖\n"
            "• Отвечать на вопросы о расписании 🧠\n"
            "• Оценивать сложность учебных дней 📊\n"
            "• Экспортировать расписание в календарь 📅\n"
            "• **Присылать утренние напоминания** ⏰\n\n"
            "📎 **Поддерживаемые форматы расписания:**\n"
            "• Excel (.xlsx, .xls) - **рекомендуется**\n"
            "• PDF документы\n"
            "• Фотографии расписания\n\n"
            "**🔄 Автоматическая обработка замен:**\n"
            "Просто перешлите сообщение от учителя, например:\n"
            "\"Ребята, завтра 5-м уроком вместо физики будет история в 302 кабинете\"\n\n"
            "**➕➖ Управление расписанием:**\n"
            "• \"Добавь урок в понедельник 3-м уроком математику в 201 кабинете\"\n"
            "• \"Удали урок во вторник 2-й урок\"\n"
            "• \"Отмени урок в среду физику\"\n\n"
            "**💡 Примеры вопросов о расписании:**\n"
            "• \"Какой завтра первый урок?\"\n"
            "• \"В каком кабинете биология?\"\n"
            "• \"Когда у нас окно?\"\n"
            "• \"Сколько уроков в пятницу?\"\n\n"
            "Выбери действие ниже или просто напиши свой вопрос!",
            reply_markup=reply_markup
        )

    async def handle_message(self, update: Update, context: CallbackContext):
        """Обработка текстовых сообщений"""
        user_message = update.message.text
        user_id = update.effective_user.id

        # Показываем индикатор набора сообщения
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Сначала проверяем, не является ли сообщение подтверждением замены
        if 'pending_replacement' in context.user_data:
            await self.handle_replace_confirmation(update, context, user_message)
            return

        # Затем проверяем, не является ли сообщение уведомлением о заменах
        if self.is_replacement_message(user_message):
            await self.handle_replacement_message(update, context, user_message)
            return

        # Проверяем команды управления расписанием
        if self.is_add_lesson_command(user_message):
            await self.handle_add_lesson(update, context, user_message)
            return

        if self.is_remove_lesson_command(user_message):
            await self.handle_remove_lesson(update, context, user_message)
            return

        # Обработка кнопок быстрого доступа
        if user_message == "📚 Помощь с учебой":
            response = (
                "По каким предметам тебе нужна помощь?\n\n"
                "Популярные предметы:\n"
                "• Математика\n• Физика\n• Русский язык\n"
                "• История\n• Английский язык\n• Химия\n"
                "• Биология\n• Информатика\n\n"
                "Или задай конкретный вопрос!"
            )
        elif user_message == "🤖 Задать вопрос":
            response = "Задай свой вопрос, и я постараюсь помочь! 🤔"
        elif user_message == "📅 Моё расписание":
            await self.show_schedule(update, context)
            return
        elif user_message == "📤 Загрузить расписание":
            response = (
                "📎 **Загрузите расписание:**\n\n"
                "Поддерживаемые форматы:\n"
                "• **Excel** (.xlsx, .xls) - **рекомендуется**\n"
                "• **PDF** - текстовые документы с расписанием\n"
                "• **Фото** - четкие фотографии бумажного расписания\n\n"
                "💡 **Советы:**\n"
                "• Для Excel используйте шаблон (кнопка «Скачать шаблон»)\n"
                "• Для PDF убедитесь, что текст можно выделить\n"
                "• Для фото - хорошее освещение и прямой угол\n\n"
                "Просто отправьте файл или фото в этот чат!"
            )
        elif user_message == "📋 Скачать шаблон":
            await self.send_template(update, context)
            return
        elif user_message == "📊 Оценить завтра":
            await self.analyze_tomorrow(update, context)
            return
        elif user_message == "➕ Добавить урок":
            response = (
                "➕ **Добавление урока в расписание:**\n\n"
                "Отправьте сообщение в формате:\n"
                "\"Добавь урок в [день] [номер] уроком [предмет]\"\n\n"
                "💡 **Примеры:**\n"
                "• \"Добавь урок в понедельник 3-м уроком математику\"\n"
                "• \"Запиши урок во вторник 5-м уроком физику в 301 кабинете\"\n"
                "• \"Новый урок в среду 2-й урок английский\"\n\n"
                "📝 **Доступные дни:**\n"
                "Понедельник, Вторник, Среда, Четверг, Пятница, Суббота\n\n"
                "🔢 **Номера уроков:** 1-8\n\n"
                "🏫 **Предметы:**\n"
                "Математика, Физика, Химия, Биология, История, География,\n"
                "Английский, Русский, Литература, Информатика, Физкультура"
            )
        elif user_message == "➖ Удалить урок":
            response = (
                "➖ **Удаление урока из расписания:**\n\n"
                "Отправьте сообщение в формате:\n"
                "\"Удали урок в [день] [номер] урок\"\n"
                "ИЛИ\n"
                "\"Удали урок в [день] [предмет]\"\n\n"
                "💡 **Примеры:**\n"
                "• \"Удали урок в понедельник 3-й урок\"\n"
                "• \"Убери урок во вторник физику\"\n"
                "• \"Отмени урок в среду 2-й урок\"\n\n"
                "📝 **Доступные дни:**\n"
                "Понедельник, Вторник, Среда, Четверг, Пятница, Суббота"
            )
        elif user_message == "📅 Экспорт в календарь":
            await self.export_calendar(update, context)
            return
        elif user_message in ["📅 Экспорт расписания (4 недели)", "⏰ Ежедневные напоминания"]:
            await self.handle_calendar_export(update, context)
            return
        elif user_message == "🔙 Назад":
            # Возвращаем главное меню
            await self.start(update, context)
            return
        elif user_message == "📈 Статистика":
            await self.show_stats(update, context)
            return
        elif user_message == "ℹ️ О боте":
            response = (
                "🤖 **Информация о боте:**\n\n"
                "• Использует нейросеть GigaChat для ответов\n"
                "• Читает расписание из Excel, PDF и фото\n"
                "• Хранит ваше расписание\n"
                "• Помогает с учебой и планированием\n"
                "• **Автоматически обрабатывает замены уроков** 🔄\n"
                "• **Добавляет и удаляет уроки** ➕➖\n"
                "• Умный анализ расписания (RAG) 🧠\n"
                "• Оценивает сложность учебных дней 📊\n"
                "• Экспортирует в календарь с напоминаниями 📅\n"
                "• Предоставляет шаблоны для расписания 📋\n"
                "• **Присылает утренние напоминания в 7:00** ⏰\n"
                "• Сохраняет историю диалогов\n"
                "• Работает 24/7\n\n"
                "💡 **Примеры вопросов о расписании:**\n"
                "• \"Какой завтра первый урок?\"\n"
                "• \"В каком кабинете биология?\"\n"
                "• \"Когда у нас окно?\"\n"
                "• \"Сколько уроков в пятницу?\"\n\n"
                "🔄 **Автоматическая обработка замен:**\n"
                "Просто перешлите сообщение от учителя!\n"
                "Пример: \"Завтра вместо физики будет история\"\n\n"
                "➕➖ **Управление расписанием:**\n"
                "• \"Добавь урок в понедельник 3-м уроком математику\"\n"
                "• \"Удали урок во вторник 2-й урок\"\n"
                "• Бот проверяет занятость слотов и предлагает замену\n\n"
                "Просто напиши свой вопрос или загрузи расписание!"
            )
        else:
            # Проверяем, относится ли вопрос к расписанию
            if self.is_schedule_question(user_message):
                await self.handle_schedule_query(update, context, user_message)
                return
            else:
                # Отправляем запрос в GigaChat
                response = self.gigachat.send_message(user_message)

                if not response:
                    response = (
                        "❌ К сожалению, GigaChat временно недоступен.\n\n"
                        "Попробуй:\n"
                        "• Переформулировать вопрос\n"
                        "• Задать вопрос позже\n"
                        "• Использовать кнопки быстрого доступа\n"
                        "• Загрузить расписание для работы с ним"
                    )
                else:
                    # Сохраняем диалог в базу данных
                    self.save_conversation(user_id, user_message, response)

        await update.message.reply_text(response)

    def is_schedule_question(self, question: str) -> bool:
        """Проверка, относится ли вопрос к расписанию"""
        schedule_keywords = [
            'урок', 'расписание', 'кабинет', 'учитель', 'предмет',
            'когда', 'во сколько', 'где', 'сколько уроков',
            'окно', 'перерыв', 'первый', 'второй', 'третий',
            'четвертый', 'пятый', 'шестой', 'седьмой', 'восьмой',
            'понедельник', 'вторник', 'среда', 'четверг', 'пятница',
            'суббота', 'сегодня', 'завтра', 'послезавтра'
        ]

        question_lower = question.lower()
        return any(keyword in question_lower for keyword in schedule_keywords)

    async def send_template(self, update: Update, context: CallbackContext):
        """Отправка шаблона Excel файла"""
        try:
            # Создаем пример данных
            data = {
                'День': ['Понедельник', 'Понедельник', 'Понедельник', 'Вторник', 'Вторник'],
                'Номер_урока': [1, 2, 3, 1, 2],
                'Время': ['08:00-08:45', '09:00-09:45', '10:00-10:45', '08:00-08:45', '09:00-09:45'],
                'Предмет': ['Математика', 'Русский язык', 'Физика', 'История', 'Химия'],
                'Кабинет': ['201', '105', '301', '208', '401'],
                'Учитель': ['Иванова А.П.', 'Петрова И.С.', 'Сидоров В.П.', 'Козлова М.И.', 'Николаев С.В.']
            }

            df = pd.DataFrame(data)

            # Создаем Excel файл в памяти
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Расписание', index=False)
            output.seek(0)

            await update.message.reply_document(
                document=InputFile(output, filename='шаблон_расписания.xlsx'),
                caption=(
                    "📋 **Шаблон для заполнения расписания**\n\n"
                    "Заполните таблицу по образцу и загрузите файл обратно в бота.\n\n"
                    "💡 **Советы:**\n"
                    "• Сохраняйте названия столбцов\n"
                    "• Используйте стандартные названия дней\n"
                    "• Указывайте время в формате ЧЧ:ММ-ЧЧ:ММ\n"
                    "• После заполнения отправьте файл боту\n\n"
                    "📝 **Структура столбцов:**\n"
                    "• **День** - Понедельник, Вторник...\n"
                    "• **Номер_урока** - 1, 2, 3...\n"
                    "• **Время** - 08:00-08:45\n"
                    "• **Предмет** - Математика, Физика...\n"
                    "• **Кабинет** - номер кабинета\n"
                    "• **Учитель** - ФИО преподавателя"
                )
            )
            logger.info("✅ Шаблон расписания отправлен пользователю")

        except Exception as e:
            logger.error(f"❌ Ошибка создания шаблона: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при создании шаблона. Попробуйте позже."
            )

    async def handle_schedule_query(self, update: Update, context: CallbackContext, question: str):
        """Обработка вопросов о расписании с помощью RAG системы"""
        user = update.effective_user

        # Парсим вопрос
        entities = self.rag_system.parse_question(question)
        day = entities.get('day')

        # Если день не указан, используем сегодняшний
        if not day:
            day = self.rag_system._get_day_by_offset(0)
            entities['day'] = day

        # Получаем расписание на нужный день
        lessons = self.get_schedule(user.id, day)

        if not lessons:
            await update.message.reply_text(
                f"❌ В расписании нет информации на {day}.\n\n"
                "Загрузите расписание через кнопку «📤 Загрузить расписание»"
            )
            return

        # Генерируем точный ответ
        response = self.rag_system.generate_precise_answer(entities, lessons, day)
        await update.message.reply_text(response)

    async def analyze_tomorrow(self, update: Update, context: CallbackContext):
        """Анализ сложности завтрашнего дня"""
        user = update.effective_user

        # Определяем завтрашний день
        tomorrow_day = self.rag_system._get_day_by_offset(1)

        # Получаем расписание на завтра
        lessons = self.get_schedule(user.id, tomorrow_day)

        if not lessons:
            await update.message.reply_text(
                f"📅 На {tomorrow_day} у тебя нет уроков по расписанию! 🎉\n\n"
                "Отличный день для отдыха или занятий по интересам!"
            )
            return

        # Преобразуем в нужный формат для анализатора
        formatted_lessons = []
        for day, lesson_num, start_time, subject, room, teacher in lessons:
            formatted_lessons.append({
                'day': day,
                'lesson_number': lesson_num,
                'start_time': start_time,
                'subject': subject,
                'room': room,
                'teacher': teacher
            })

        # Анализируем сложность дня
        analysis = self.analyzer.calculate_day_complexity(formatted_lessons)

        # Формируем ответ
        response = f"📊 **Анализ завтрашнего дня ({tomorrow_day}):**\n\n"
        response += f"⚡ **Сложность: {analysis['score']}/10** ({analysis['level']})\n"
        response += f"📚 Уроков: {analysis['lesson_count']}\n"

        if analysis['test_count'] > 0:
            response += f"✏️ Контрольные: {analysis['test_count']}\n"

        response += "\n**💡 Рекомендации:**\n"
        for rec in analysis['recommendations']:
            response += f"• {rec}\n"

        # Добавляем расписание для контекста
        response += f"\n**📅 Расписание на {tomorrow_day}:**\n"
        for lesson in formatted_lessons:
            time_display = f"🕒 {lesson['start_time']}" if lesson['start_time'] else f"{lesson['lesson_number']}."
            response += f"  {time_display} {lesson['subject']}\n"

        await update.message.reply_text(response)

    async def export_calendar(self, update: Update, context: CallbackContext):
        """Экспорт расписания в календарь"""
        user = update.effective_user
        lessons_data = self.get_schedule(user.id)

        if not lessons_data:
            await update.message.reply_text(
                "❌ У вас еще нет расписания.\n\n"
                "Сначала загрузите расписание через кнопку «📤 Загрузить расписание»"
            )
            return

        # Преобразуем в нужный формат
        lessons = []
        for day, lesson_num, start_time, subject, room, teacher in lessons_data:
            lessons.append({
                'day': day,
                'lesson_number': lesson_num,
                'start_time': start_time,
                'subject': subject,
                'room': room,
                'teacher': teacher
            })

        # Создаем клавиатуру для выбора типа экспорта
        keyboard = [
            [KeyboardButton("📅 Экспорт расписания (4 недели)")],
            [KeyboardButton("⏰ Ежедневные напоминания")],
            [KeyboardButton("🔙 Назад")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await update.message.reply_text(
            "📅 **Экспорт в календарь:**\n\n"
            "Выберите тип экспорта:\n"
            "• **Расписание** - все уроки на 4 недели вперед\n"
            "• **Напоминания** - ежедневные уведомления о расписании\n\n"
            "После скачивания файла:\n"
            "1. Откройте файл на телефоне\n"
            "2. Выберите «Добавить в календарь»\n"
            "3. События появятся в вашем календаре",
            reply_markup=reply_markup
        )

    async def handle_calendar_export(self, update: Update, context: CallbackContext):
        """Обработка выбора типа экспорта календаря"""
        user = update.effective_user
        user_message = update.message.text
        lessons_data = self.get_schedule(user.id)

        if not lessons_data:
            await update.message.reply_text("❌ Сначала загрузите расписание")
            return

        # Преобразуем в нужный формат
        lessons = []
        for day, lesson_num, start_time, subject, room, teacher in lessons_data:
            lessons.append({
                'day': day,
                'lesson_number': lesson_num,
                'start_time': start_time,
                'subject': subject,
                'room': room,
                'teacher': teacher
            })

        try:
            if user_message == "📅 Экспорт расписания (4 недели)":
                # Генерируем полное расписание на 4 недели
                ics_content = self.calendar_exporter.generate_ics_file(lessons, weeks=4)
                filename = "school_schedule.ics"
                caption = (
                    "📅 **Ваше расписание на 4 недели**\n\n"
                    "Как добавить в календарь:\n"
                    "• **Android:** Откройте файл → Выберите «Календарь»\n"
                    "• **iPhone:** Нажмите «Поделиться» → «Копировать в Календарь»\n"
                    "• **Компьютер:** Импортируйте в Google Calendar/Outlook\n\n"
                    "События включают напоминания за 15 минут!"
                )

            elif user_message == "⏰ Ежедневные напоминания":
                # Генерируем ежедневные напоминания
                ics_content = self.calendar_exporter.generate_daily_reminders(lessons, days=30)
                filename = "school_reminders.ics"
                caption = (
                    "⏰ **Ежедневные напоминания о расписании**\n\n"
                    "Каждое утро в 7:00 вы будете получать уведомление\n"
                    "с расписанием на текущий день.\n\n"
                    "Как добавить:\n"
                    "• Откройте файл на телефоне\n"
                    "• Выберите «Добавить в календарь»\n"
                    "• Разрешите уведомления"
                )

            else:
                return

            # Отправляем файл
            file_obj = io.BytesIO(ics_content)
            file_obj.name = filename

            await update.message.reply_document(
                document=InputFile(file_obj, filename=filename),
                caption=caption,
                filename=filename
            )

            logger.info(f"✅ Пользователь {user.id} экспортировал календарь: {user_message}")

        except Exception as e:
            logger.error(f"❌ Ошибка экспорта календаря: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при создании файла календаря.\n"
                "Попробуйте позже или обратитесь к разработчику."
            )

    async def send_morning_reminder(self, context: ContextTypes.DEFAULT_TYPE):
        """Отправка утреннего напоминания в 7:00"""
        try:
            # Получаем список пользователей с включенными уведомлениями
            users = self.get_users_with_morning_reminders()

            if not users:
                logger.info("⏰ Нет пользователей для утренних уведомлений")
                return

            # Получаем сегодняшний день
            today_day = self.rag_system._get_day_by_offset(0)

            for user_id in users:
                try:
                    # Получаем расписание пользователя на сегодня
                    lessons = self.get_schedule(user_id, today_day)

                    if not lessons:
                        continue

                    # Формируем сообщение с пожеланиями и расписанием
                    message = "🌅 **Доброе утро!** ☀️\n\n"
                    message += "💫 Пусть этот день будет полон успехов и новых достижений!\n\n"

                    if lessons:
                        message += f"📅 **Ваше расписание на сегодня ({today_day}):**\n\n"

                        for day, lesson_num, start_time, subject, room, teacher in sorted(lessons, key=lambda x: x[1]):
                            time_display = f"🕒 {start_time}" if start_time else f"{lesson_num}."
                            room_display = f" 🚪 {room}" if room else ""
                            teacher_display = f" 👨‍🏫 {teacher}" if teacher else ""
                            message += f"  {time_display} {subject}{room_display}{teacher_display}\n"

                        # Добавляем мотивационное сообщение
                        lesson_count = len(lessons)
                        if lesson_count >= 6:
                            message += "\n💪 Сегодня насыщенный день! Не забывайте делать перерывы и пить воду! 💧"
                        elif lesson_count <= 3:
                            message += "\n😊 Легкий день - отличная возможность заняться чем-то интересным! ✨"
                        else:
                            message += "\n📚 Хорошего дня и успехов в учебе! 🎯"

                    # Отправляем сообщение
                    await context.bot.send_message(chat_id=user_id, text=message)
                    logger.info(f"⏰ Утреннее уведомление отправлено пользователю {user_id}")

                    # Небольшая задержка между отправками, чтобы не превысить лимиты
                    await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error(f"❌ Ошибка отправки уведомления пользователю {user_id}: {e}")

            logger.info("✅ Утренние уведомления отправлены")

        except Exception as e:
            logger.error(f"❌ Ошибка в задаче утренних уведомлений: {e}")

    async def show_schedule(self, update: Update, context: CallbackContext):
        """Показать расписание пользователя"""
        user = update.effective_user
        logger.info(f"📅 Пользователь {user.id} запросил расписание")

        lessons = self.get_schedule(user.id)

        if not lessons:
            await update.message.reply_text(
                "📭 У вас еще нет расписания.\n\n"
                "Загрузите расписание:\n"
                "• Excel файл (.xlsx, .xls) - **рекомендуется**\n"
                "• PDF документ\n"
                "• Фото расписания\n\n"
                "Используйте кнопку «📤 Загрузить расписание» или «📋 Скачать шаблон»"
            )
            return

        # Группируем уроки по дням
        schedule_by_day = {}
        for day, lesson_num, start_time, subject, room, teacher in lessons:
            if day not in schedule_by_day:
                schedule_by_day[day] = []
            schedule_by_day[day].append((lesson_num, start_time, subject, room, teacher))

        # Формируем ответ
        response = "📅 **Ваше расписание:**\n\n"

        for day in ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']:
            if day in schedule_by_day:
                response += f"**{day}:**\n"
                day_lessons = sorted(schedule_by_day[day], key=lambda x: x[0])
                for lesson_num, start_time, subject, room, teacher in day_lessons:
                    time_display = f"🕒 {start_time}" if start_time else f"{lesson_num}."
                    room_display = f" 🚪 {room}" if room else ""
                    teacher_display = f" 👨‍🏫 {teacher}" if teacher else ""
                    response += f"  {time_display} {subject}{room_display}{teacher_display}\n"
                response += "\n"

        await update.message.reply_text(response)
        logger.info(f"✅ Расписание отправлено пользователю {user.id}")

    async def show_stats(self, update: Update, context: CallbackContext):
        """Показать статистику"""
        user = update.effective_user

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Статистика диалогов
        cursor.execute('SELECT COUNT(*) FROM conversations WHERE user_id = ?', (user.id,))
        conv_count = cursor.fetchone()[0]

        # Статистика расписания
        cursor.execute('SELECT COUNT(*) FROM schedule WHERE user_id = ?', (user.id,))
        lessons_count = cursor.fetchone()[0]

        # Статистика файлов
        cursor.execute('SELECT COUNT(*) FROM uploaded_files WHERE user_id = ?', (user.id,))
        files_count = cursor.fetchone()[0]

        conn.close()

        response = (
            f"📊 **Ваша статистика:**\n\n"
            f"💬 Диалоги: {conv_count}\n"
            f"📅 Уроков в расписании: {lessons_count}\n"
            f"📎 Загружено файлов: {files_count}\n\n"
        )

        if lessons_count > 0:
            lessons = self.get_schedule(user.id)
            subjects = set(lesson[3] for lesson in lessons)
            days = set(lesson[0] for lesson in lessons)
            response += f"📚 Предметов: {len(subjects)}\n"
            response += f"📅 Дней с уроками: {len(days)}"

        await update.message.reply_text(response)

    async def handle_document(self, update: Update, context: CallbackContext):
        """Обработка загруженных документов"""
        user = update.effective_user
        document = update.message.document

        # Получаем информацию о файле
        file = await document.get_file()
        file_name = document.file_name
        file_size = document.file_size
        file_extension = file_name.split('.')[-1].lower() if '.' in file_name else ''

        await update.message.reply_text(f"📥 Загружаю файл: {file_name}...")

        try:
            # Скачиваем файл
            file_content = await file.download_as_bytearray()
            logger.info(f"📄 Файл {file_name} загружен, размер: {len(file_content)} байт")

            # Парсим файл в зависимости от формата
            lessons = []
            if file_extension in ['xlsx', 'xls']:
                await update.message.reply_text("🔍 Читаю Excel файл...")
                lessons = self.parser.parse_excel(bytes(file_content))
            elif file_extension == 'pdf':
                await update.message.reply_text("🔍 Читаю PDF документ...")
                lessons = self.parser.parse_pdf(bytes(file_content))
            else:
                await update.message.reply_text(
                    f"❌ Формат .{file_extension} не поддерживается.\n\n"
                    "Поддерживаемые форматы:\n"
                    "• Excel: .xlsx, .xls\n"
                    "• PDF: .pdf\n"
                    "• Изображения: .jpg, .png (отправьте как фото)"
                )
                return

            if lessons:
                # Сохраняем расписание
                success = self.save_schedule(user.id, lessons)
                if success:
                    self.save_uploaded_file(user.id, file_name, file_extension, file_size)

                    # Формируем статистику
                    days = set(lesson['day'] for lesson in lessons)
                    subjects = set(lesson['subject'] for lesson in lessons)

                    response = (
                        f"✅ Расписание успешно загружено!\n\n"
                        f"📊 Статистика:\n"
                        f"• Уроков: {len(lessons)}\n"
                        f"• Дней: {len(days)}\n"
                        f"• Предметов: {len(subjects)}\n"
                        f"• Дни недели: {', '.join(days)}\n\n"
                        f"💡 Теперь вы можете:\n"
                        f"• Спрашивать: «Какой завтра первый урок?»\n"
                        f"• Получать утренние напоминания в 7:00 ⏰\n"
                        f"• Использовать кнопки ниже"
                    )
                else:
                    response = "❌ Произошла ошибка при сохранении расписания в базу данных."
            else:
                response = (
                    "❌ Не удалось распознать расписание в файле.\n\n"
                    "Возможные причины:\n"
                    "• Неправильный формат файла\n"
                    "• Файл не содержит расписания в ожидаемом формате\n"
                    "• Для Excel: используйте правильные названия колонок\n\n"
                    "📋 Используйте кнопку «Скачать шаблон» для получения готового шаблона"
                )

            await update.message.reply_text(response)

        except Exception as e:
            logger.error(f"❌ Ошибка обработки файла: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при обработке файла. Попробуйте другой файл или используйте шаблон."
            )

    async def handle_photo(self, update: Update, context: CallbackContext):
        """Обработка загруженных фото"""
        user = update.effective_user

        # Берем фото наибольшего качества (последнее в списке)
        photo = update.message.photo[-1]

        await update.message.reply_text("📥 Загружаю фото...")

        try:
            # Скачиваем фото
            file = await photo.get_file()
            file_content = await file.download_as_bytearray()
            logger.info(f"🖼️ Фото загружено, размер: {len(file_content)} байт")

            await update.message.reply_text("🔍 Распознаю текст на фото...")

            # Парсим изображение
            lessons = self.parser.parse_image(bytes(file_content))

            if lessons:
                # Сохраняем расписание
                success = self.save_schedule(user.id, lessons)
                if success:
                    self.save_uploaded_file(user.id, "schedule_photo.jpg", "jpg", len(file_content))

                    # Формируем статистику
                    days = set(lesson['day'] for lesson in lessons)

                    response = (
                        f"✅ Расписание успешно распознано!\n\n"
                        f"📊 Статистика:\n"
                        f"• Уроков: {len(lessons)}\n"
                        f"• Дней: {len(days)}\n"
                        f"• Дни недели: {', '.join(days)}\n\n"
                        f"💡 Теперь вы можете:\n"
                        f"• Спрашивать: «Какой завтра первый урок?»\n"
                        f"• Получать утренние напоминания в 7:00 ⏰\n"
                        f"• Использовать кнопки ниже"
                    )
                else:
                    response = "❌ Произошла ошибка при сохранении расписания в базу данных."
            else:
                response = (
                    "❌ Не удалось распознать расписание на фото.\n\n"
                    "Советы для лучшего распознавания:\n"
                    "• Сфотографируйте при хорошем освещении\n"
                    "• Держите камеру прямо над расписанием\n"
                    "• Убедитесь, что текст четкий и не размытый\n"
                    "• Попробуйте сфотографировать ближе к тексту\n\n"
                    "📋 Для лучшего результата используйте Excel шаблон (кнопка «Скачать шаблон»)"
                )

            await update.message.reply_text(response)

        except Exception as e:
            logger.error(f"❌ Ошибка обработки фото: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при обработке фото. Попробуйте другое изображение или используйте шаблон."
            )

    async def error_handler(self, update: Update, context: CallbackContext):
        """Обработка ошибок"""
        logger.error(f"Ошибка: {context.error}")

        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ Произошла ошибка. Пожалуйста, попробуйте еще раз."
            )

    def run(self):
        """Запуск бота"""
        # Создаем приложение
        application = Application.builder().token(TELEGRAM_TOKEN).build()

        # Получаем job_queue
        job_queue = application.job_queue

        if job_queue is None:
            logger.warning("❌ JobQueue не доступен. Утренние уведомления отключены.")
        else:
            # Добавляем задачу для утренних уведомлений
            try:
                # Утреннее уведомление в 7:00 каждый день
                job_queue.run_daily(
                    self.send_morning_reminder,
                    time=time(hour=7, minute=0, second=0),  # 7:00 утра
                    days=(0, 1, 2, 3, 4, 5, 6),  # Все дни недели
                    name="morning_reminder"
                )
                logger.info("⏰ Утренние уведомления настроены на 7:00")
            except Exception as e:
                logger.error(f"❌ Ошибка настройки уведомлений: {e}")

        # Добавляем обработчики
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        application.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))

        # Обработчик ошибок
        application.add_error_handler(self.error_handler)

        # Запускаем бота
        logger.info("🤖 Бот запускается...")
        application.run_polling()


def main():
    """Основная функция"""
    # Проверяем наличие необходимых библиотек
    try:
        import pandas as pd
        import pdfplumber
        import pytesseract
        from PIL import Image
        from icalendar import Calendar, Event
    except ImportError as e:
        print(f"❌ Не установлены необходимые библиотеки: {e}")
        print("Установите их командой:")
        print("pip install pandas pdfplumber pytesseract pillow python-telegram-bot icalendar")
        return

    # Создаем и запускаем бота
    bot = TelegramBot()
    bot.run()


if __name__ == '__main__':
    main()