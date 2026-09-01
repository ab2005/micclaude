/**
 * Interface translations.
 *
 * The page follows the spoken language the server was started with, so
 * `micclaude --lang ru` gives a Russian interface as well as Russian
 * recognition. A viewer can override that in the settings panel.
 */

export const LANGUAGES = Object.freeze({ en: 'English', ru: 'Русский' });

export const STRINGS = Object.freeze({
  en: {
    'tag.localStt': 'local stt',
    'tag.cloudStt': 'cloud stt',

    'status.idle': 'Not listening',
    'status.listening': 'Listening',
    'status.hearing': 'Hearing you',
    'status.working': 'Transcribing',
    'status.thinking': 'Claude is thinking',
    'status.speaking': 'Speaking',
    'status.armed': 'Go ahead, I am listening',
    'status.error': 'Something went wrong',

    'button.listen': 'Start listening',
    'button.stopListening': 'Stop listening',
    'button.ask': 'Ask now',
    'button.ask.title': 'Ask the next thing you say (Space)',
    'button.cancel': 'Cancel',
    'button.settings': 'Settings',
    'button.send': 'Ask',
    'button.newSession': 'Start a new Claude session',

    'meter.title': 'Microphone level. The mark shows the speech threshold.',

    'empty.title': 'Everything you say is transcribed here.',
    'empty.lead': 'Claude only answers when you address it:',
    'empty.example': '{wake}, what does this error mean?',
    'empty.hint.arm': 'Say just "{wake}" and the next thing you say is the question.',
    'empty.hint.cancel': 'Say "{cancel}" to cancel a pending question.',
    'empty.hint.keyBefore': 'Press',
    'empty.hint.keyAfter': 'when the wake word keeps getting missed.',

    'settings.title': 'Settings',
    'settings.microphone': 'Microphone',
    'settings.device': 'Input device',
    'settings.device.default': 'System default',
    'settings.sensitivity': 'Sensitivity',
    'settings.sensitivity.note':
      'Speech is detected above this level. The bar in the header shows the current level, so watch it while you talk and while the room is quiet.',
    'settings.silence': 'End of phrase',
    'units.ms': 'ms',
    'settings.silence.note': 'How long a pause has to be before your sentence is sent off.',

    'settings.wake': 'Wake word',
    'settings.wake.label': 'Words that address Claude',
    'settings.wake.note': 'Comma separated. Close mishearings count too.',
    'settings.wake.requirePrefix': 'Require a greeting ("hey claude", never a bare "claude")',

    'settings.replies': 'Replies',
    'settings.speak': 'Speak replies out loud',
    'settings.voice': 'Voice',
    'settings.voice.default': 'Browser default',
    'settings.rate': 'Speed',
    'settings.context': 'Transcript lines sent as context',
    'settings.context.note':
      'Recent speech Claude sees with each question, so "what did I just say?" works. Set to 0 to send the question alone.',

    'settings.interface': 'Interface',
    'settings.uiLanguage': 'Language',
    'settings.uiLanguage.auto': 'Follow the server',

    'settings.session': 'Session',
    'settings.session.note':
      'Follow-up questions continue the same conversation until you reset it.',

    'facts.model': 'Speech model',
    'facts.workingDir': 'Claude runs in',
    'facts.claudeModel': 'Claude model',
    'facts.transcript': 'Transcript',
    'facts.transcriptOff': 'not saved',
    'facts.default': 'default',

    'compose.placeholder': 'Or type a question for Claude',
    'footnote.local': 'Audio is transcribed on this machine and never leaves it.',
    'footnote.cloud': 'Audio is sent to your configured transcription API.',
    'footnote.saved': 'The text is saved on this machine.',

    'notice.cancelled': 'Cancelled.',
    'notice.newSession': 'Started a new Claude session. Earlier questions are forgotten.',
    'notice.flag': 'Noticed: {rule}',
    'notice.noAnswer': '(no answer)',
    'error.noCapture': 'This browser cannot capture audio. Chrome, Edge and Safari can.',
    'error.micDenied':
      'Microphone permission was denied. Allow it in the address bar, then start again.',
    'error.micFailed': 'Could not open the microphone: {error}',
    'error.server': 'Could not reach the micclaude server: {error}',
    'error.health': 'Health check failed: {error}',
  },

  ru: {
    'tag.localStt': 'распознавание локально',
    'tag.cloudStt': 'распознавание в облаке',

    'status.idle': 'Не слушаю',
    'status.listening': 'Слушаю',
    'status.hearing': 'Слышу вас',
    'status.working': 'Распознаю',
    'status.thinking': 'Клавдий думает',
    'status.speaking': 'Говорю',
    'status.armed': 'Говорите, я слушаю',
    'status.error': 'Что-то пошло не так',

    'button.listen': 'Начать слушать',
    'button.stopListening': 'Перестать слушать',
    'button.ask': 'Спросить',
    'button.ask.title': 'Следующая фраза станет вопросом (пробел)',
    'button.cancel': 'Отмена',
    'button.settings': 'Настройки',
    'button.send': 'Спросить',
    'button.newSession': 'Начать новую сессию',

    'meter.title': 'Уровень микрофона. Метка — порог, выше которого начинается речь.',

    'empty.title': 'Здесь появляется всё, что вы говорите.',
    'empty.lead': 'Ассистент отвечает, только когда вы обращаетесь к нему:',
    'empty.example': '{wake}, что означает эта ошибка?',
    'empty.hint.arm': 'Скажите просто «{wake}» — вопросом станет следующая фраза.',
    'empty.hint.cancel': 'Скажите «{cancel}», чтобы отменить заданный вопрос.',
    'empty.hint.keyBefore': 'Нажмите',
    'empty.hint.keyAfter': ', если обращение постоянно не распознаётся.',

    'settings.title': 'Настройки',
    'settings.microphone': 'Микрофон',
    'settings.device': 'Устройство ввода',
    'settings.device.default': 'Системное по умолчанию',
    'settings.sensitivity': 'Чувствительность',
    'settings.sensitivity.note':
      'Речью считается всё, что громче этого порога. Полоска в шапке показывает текущий уровень — посмотрите на неё в тишине и когда говорите.',
    'settings.silence': 'Конец фразы',
    'units.ms': 'мс',
    'settings.silence.note': 'Насколько длинной должна быть пауза, чтобы фраза ушла на распознавание.',

    'settings.wake': 'Обращение',
    'settings.wake.label': 'Слова, которыми вы зовёте ассистента',
    'settings.wake.note': 'Через запятую. Близкие ослышки засчитываются тоже.',
    'settings.wake.requirePrefix': 'Требовать приветствие («эй, Клавдий», а не просто «Клавдий»)',

    'settings.replies': 'Ответы',
    'settings.speak': 'Произносить ответы вслух',
    'settings.voice': 'Голос',
    'settings.voice.default': 'Голос браузера',
    'settings.rate': 'Скорость',
    'settings.context': 'Строк расшифровки в контексте',
    'settings.context.note':
      'Сколько последних фраз уходит вместе с вопросом, чтобы работало «что я только что сказал?». Ноль — отправлять один вопрос.',

    'settings.interface': 'Интерфейс',
    'settings.uiLanguage': 'Язык',
    'settings.uiLanguage.auto': 'Как на сервере',

    'settings.session': 'Сессия',
    'settings.session.note':
      'Следующие вопросы продолжают тот же разговор, пока вы его не сбросите.',

    'facts.model': 'Модель распознавания',
    'facts.workingDir': 'Рабочая папка',
    'facts.claudeModel': 'Модель Claude',
    'facts.transcript': 'Расшифровка',
    'facts.transcriptOff': 'не сохраняется',
    'facts.default': 'по умолчанию',

    'compose.placeholder': 'Или напишите вопрос',
    'footnote.local': 'Звук распознаётся на этой машине и никуда не уходит.',
    'footnote.cloud': 'Звук уходит в указанный вами сервис распознавания.',
    'footnote.saved': 'Текст сохраняется на этой машине.',

    'notice.cancelled': 'Отменено.',
    'notice.newSession': 'Начата новая сессия. Прежние вопросы забыты.',
    'notice.flag': 'Замечено: {rule}',
    'notice.noAnswer': '(нет ответа)',
    'error.noCapture': 'Этот браузер не умеет записывать звук. Подойдут Chrome, Edge или Safari.',
    'error.micDenied':
      'Доступ к микрофону запрещён. Разрешите его в адресной строке и начните снова.',
    'error.micFailed': 'Не удалось открыть микрофон: {error}',
    'error.server': 'Сервер micclaude недоступен: {error}',
    'error.health': 'Проверка сервера не прошла: {error}',
  },
});

export const FALLBACK_LANGUAGE = 'en';

/**
 * Pick the interface language.
 *
 * A stored choice wins; otherwise follow the server's spoken language, then
 * the browser's preferences, then English.
 */
export function resolveLanguage({ stored, server, navigatorLanguages = [] } = {}) {
  const candidates = [stored, server, ...navigatorLanguages];
  for (const candidate of candidates) {
    if (!candidate) continue;
    const base = String(candidate).toLowerCase().split('-')[0];
    if (base in STRINGS) return base;
  }
  return FALLBACK_LANGUAGE;
}

/** Build a lookup function for one language, falling back to English. */
export function createTranslator(language) {
  const table = STRINGS[language] || STRINGS[FALLBACK_LANGUAGE];
  return function t(key, vars) {
    const template = table[key] ?? STRINGS[FALLBACK_LANGUAGE][key] ?? key;
    if (!vars) return template;
    return template.replace(/\{(\w+)\}/g, (match, name) =>
      (name in vars ? String(vars[name]) : match));
  };
}

/**
 * Translate a document fragment in place.
 *
 * Elements opt in with data-i18n (text), data-i18n-placeholder or
 * data-i18n-title, so the markup stays the single list of what needs
 * translating.
 */
export function applyTranslations(root, t, vars) {
  for (const node of root.querySelectorAll('[data-i18n]')) {
    node.textContent = t(node.dataset.i18n, vars);
  }
  for (const node of root.querySelectorAll('[data-i18n-placeholder]')) {
    node.placeholder = t(node.dataset.i18nPlaceholder, vars);
  }
  for (const node of root.querySelectorAll('[data-i18n-title]')) {
    node.title = t(node.dataset.i18nTitle, vars);
  }
}
