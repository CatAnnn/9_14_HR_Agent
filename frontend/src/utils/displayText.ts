import {
  getCurrentLanguage,
  type AppLanguage,
} from '../i18n/languageRuntime.ts';

type LocalizedText = Readonly<Record<AppLanguage, string>>;
type InternalFieldReplacement = readonly [RegExp, LocalizedText];

function localizedText(values: LocalizedText, language = getCurrentLanguage()): string {
  return values[language] || values.en;
}

const INTERNAL_FIELD_REPLACEMENTS: ReadonlyArray<InternalFieldReplacement> = [
  [/\bcontext\.dimension_config\.dimension\.id\b/gi, { 'zh-CN': '当前评估维度', en: 'current evaluation dimension', de: 'aktuelle Bewertungsdimension', ja: '現在の評価観点' }],
  [/\bcontext\.dimension_config\.intent_focus\b/gi, { 'zh-CN': '当前沟通意图的评估重点', en: 'evaluation focus for the current conversation intent', de: 'Bewertungsschwerpunkt für die aktuelle Gesprächsabsicht', ja: '現在の対話意図における評価の重点' }],
  [/\bcontext\.dimension_config\b/gi, { 'zh-CN': '当前评估标准', en: 'current evaluation criteria', de: 'aktuelle Bewertungskriterien', ja: '現在の評価基準' }],
  [/\bcontext\.career_elements_applicable\b/gi, { 'zh-CN': 'Career Elements 适用条件', en: 'Career Elements applicability', de: 'Anwendbarkeit der Career Elements', ja: 'Career Elements の適用条件' }],
  [/\bcareer_elements_applicable\b/gi, { 'zh-CN': 'Career Elements 适用条件', en: 'Career Elements applicability', de: 'Anwendbarkeit der Career Elements', ja: 'Career Elements の適用条件' }],
  [/\bcontext\.current_career_elements\b/gi, { 'zh-CN': '员工当前的 Career Elements', en: 'the employee\'s current Career Elements', de: 'aktuelle Career Elements des Mitarbeitenden', ja: '従業員が現在有する Career Elements' }],
  [/\bcurrent_career_elements\b/gi, { 'zh-CN': '员工当前的 Career Elements', en: 'the employee\'s current Career Elements', de: 'aktuelle Career Elements des Mitarbeitenden', ja: '従業員が現在有する Career Elements' }],
  [/\bcontext\.employee_profile\b|\bemployee_profile\b/gi, { 'zh-CN': '员工资料', en: 'employee profile', de: 'Mitarbeiterprofil', ja: '従業員情報' }],
  [/\bcontext\.profile\b/gi, { 'zh-CN': '员工资料', en: 'employee profile', de: 'Mitarbeiterprofil', ja: '従業員情報' }],
  [/\bcontext\.supplemental_info\b|\bsupplemental_info\b/gi, { 'zh-CN': '员工其他背景信息', en: 'additional employee context', de: 'zusätzlicher Mitarbeiterkontext', ja: '従業員に関する追加情報' }],
  [/\bcontext\.performance_context\b|\bperformance_context\b/gi, { 'zh-CN': '员工当前表现', en: 'current employee performance', de: 'aktuelle Leistung des Mitarbeitenden', ja: '従業員の現在のパフォーマンス' }],
  [/\bcontext\.intent_id\b|\bintent_id\b/gi, { 'zh-CN': '沟通意图', en: 'conversation intent', de: 'Gesprächsabsicht', ja: '対話意図' }],
  [/\bcontext\.intent\.config\b/gi, { 'zh-CN': '沟通意图规则', en: 'conversation-intent rules', de: 'Regeln für die Gesprächsabsicht', ja: '対話意図のルール' }],
  [/\bcontext\.intent\b/gi, { 'zh-CN': '沟通意图', en: 'conversation intent', de: 'Gesprächsabsicht', ja: '対話意図' }],
  [/\bcontext\.personality\b/gi, { 'zh-CN': '员工人格倾向', en: 'employee personality tendencies', de: 'Persönlichkeitstendenzen des Mitarbeitenden', ja: '従業員の性格傾向' }],
  [/\bcontext\.motivation\b/gi, { 'zh-CN': '员工诉求', en: 'employee motives', de: 'Motive des Mitarbeitenden', ja: '従業員の動機' }],
  [/\bprimary_motive_id\b/gi, { 'zh-CN': '员工的主诉求', en: 'employee primary motive', de: 'wichtigstes Motiv des Mitarbeitenden', ja: '従業員の主な動機' }],
  [/\bsecondary_motive_ids\b/gi, { 'zh-CN': '员工的辅诉求', en: 'employee secondary motives', de: 'weitere Motive des Mitarbeitenden', ja: '従業員の補助的な動機' }],
  [/\bcontext\.conversation\b/gi, { 'zh-CN': '本次对话', en: 'this conversation', de: 'dieses Gespräch', ja: '今回の対話' }],
  [/\bcontext\.retrieved_chunks\b|\bretrieved_chunks\b/gi, { 'zh-CN': '知识库参考资料', en: 'knowledge-base references', de: 'Wissensbasis-Referenzen', ja: 'ナレッジベースの参照情報' }],
  [/\borganization_unit_chunks\b/gi, { 'zh-CN': '组织信息参考资料', en: 'organization references', de: 'Organisationsreferenzen', ja: '組織情報の参照資料' }],
  [/\bculture_chunks\b/gi, { 'zh-CN': '企业文化参考资料', en: 'culture references', de: 'Unternehmenskultur-Referenzen', ja: '企業文化の参照資料' }],
  [/\bcontext\.active_skills\b|\bactive_skills\b/gi, { 'zh-CN': '专业方法参考', en: 'professional-method references', de: 'fachliche Methodenreferenzen', ja: '専門手法の参照情報' }],
  [/\bknowledge_chunk_ids\b|\bchunk_id\b/gi, { 'zh-CN': '知识来源', en: 'knowledge source', de: 'Wissensquelle', ja: '知識の出典' }],
  [/\bsource_ref\b/gi, { 'zh-CN': '来源依据', en: 'source reference', de: 'Quellenverweis', ja: '出典情報' }],
  [/\bemotion_state\b|\bcurrent_vad\b/gi, { 'zh-CN': '当前情绪状态', en: 'current emotional state', de: 'aktueller emotionaler Zustand', ja: '現在の感情状態' }],
  [/\bdimension_id\b/gi, { 'zh-CN': '评估维度', en: 'evaluation dimension', de: 'Bewertungsdimension', ja: '評価観点' }],
  [/\btask_id\b/gi, { 'zh-CN': '评估任务', en: 'evaluation task', de: 'Bewertungsaufgabe', ja: '評価タスク' }],
];

const ERROR_CODE_MESSAGES: Readonly<Record<string, string>> = {
  bad_request: '提交的信息不正确，请检查后重试。',
  browser_asr_uses_client_runtime: '当前配置使用浏览器内置语音识别。',
  capture_capacity_exceeded: '语音录音连接已满，请稍后重试。',
  final_transcription_failed: '语音转写未能完成，请重试。',
  forbidden: '当前账号没有执行此操作的权限。',
  llm_error: '模型服务暂时无法完成请求，请稍后重试。',
  no_speech_detected: '未检测到清晰语音，请重新录制。',
  not_found: '没有找到所需内容，它可能已被更新或删除。',
  parser_error: '上传内容暂时无法解析，请检查文件后重试。',
  preview_capacity_exceeded: '实时语音预览并发已满，完整录音仍在继续。',
  preview_unavailable: '实时语音预览暂不可用，完整录音仍在继续。',
  realtime_failed: '语音录音或转写失败，请重试。',
  recording_storage_failed: '无法保存录音，请稍后重试。',
  unauthorized: '登录状态已失效，请重新登录。',
  workflow_error: '当前流程状态不允许执行此操作，请刷新后重试。',
};

const ERROR_CODE_MESSAGES_ENGLISH: Readonly<Record<string, string>> = {
  bad_request: 'The submitted information is invalid. Check it and try again.',
  browser_asr_uses_client_runtime: 'The current configuration uses the browser\'s built-in speech recognition.',
  capture_capacity_exceeded: 'All speech-recording connections are currently in use. Please try again later.',
  final_transcription_failed: 'The transcription could not be completed. Please try again.',
  forbidden: 'This account does not have permission to perform this action.',
  llm_error: 'The model service could not complete the request. Please try again later.',
  no_speech_detected: 'No clear speech was detected. Please record again.',
  not_found: 'The requested content could not be found. It may have been updated or removed.',
  parser_error: 'The uploaded content could not be parsed. Check the file and try again.',
  preview_capacity_exceeded: 'Live speech-preview capacity is full. The complete recording is still continuing.',
  preview_unavailable: 'Live speech preview is temporarily unavailable. The complete recording is still continuing.',
  realtime_failed: 'Speech recording or transcription failed. Please try again.',
  recording_storage_failed: 'The recording could not be saved. Please try again later.',
  unauthorized: 'Your sign-in session has expired. Please sign in again.',
  workflow_error: 'This action is not available at the current workflow stage. Refresh and try again.',
};

const ERROR_CODE_MESSAGES_GERMAN: Readonly<Record<string, string>> = {
  bad_request: 'Die übermittelten Angaben sind ungültig. Bitte prüfen Sie sie und versuchen Sie es erneut.',
  browser_asr_uses_client_runtime: 'Die aktuelle Konfiguration verwendet die integrierte Spracherkennung des Browsers.',
  capture_capacity_exceeded: 'Derzeit sind alle Verbindungen für Sprachaufnahmen belegt. Bitte versuchen Sie es später erneut.',
  final_transcription_failed: 'Die Transkription konnte nicht abgeschlossen werden. Bitte versuchen Sie es erneut.',
  forbidden: 'Dieses Konto ist für diese Aktion nicht berechtigt.',
  llm_error: 'Der Modelldienst konnte die Anfrage nicht abschließen. Bitte versuchen Sie es später erneut.',
  no_speech_detected: 'Es wurde keine verständliche Sprache erkannt. Bitte nehmen Sie erneut auf.',
  not_found: 'Der angeforderte Inhalt wurde nicht gefunden. Möglicherweise wurde er geändert oder entfernt.',
  parser_error: 'Der hochgeladene Inhalt konnte nicht verarbeitet werden. Bitte prüfen Sie die Datei und versuchen Sie es erneut.',
  preview_capacity_exceeded: 'Die Kapazität der Live-Sprachvorschau ist ausgelastet. Die vollständige Aufnahme läuft weiter.',
  preview_unavailable: 'Die Live-Sprachvorschau ist momentan nicht verfügbar. Die vollständige Aufnahme läuft weiter.',
  realtime_failed: 'Sprachaufnahme oder Transkription fehlgeschlagen. Bitte versuchen Sie es erneut.',
  recording_storage_failed: 'Die Aufnahme konnte nicht gespeichert werden. Bitte versuchen Sie es später erneut.',
  unauthorized: 'Ihre Anmeldung ist abgelaufen. Bitte melden Sie sich erneut an.',
  workflow_error: 'Diese Aktion ist im aktuellen Prozessschritt nicht verfügbar. Bitte aktualisieren Sie die Seite und versuchen Sie es erneut.',
};

const ERROR_CODE_MESSAGES_JAPANESE: Readonly<Record<string, string>> = {
  bad_request: '送信された情報が正しくありません。内容を確認して、もう一度お試しください。',
  browser_asr_uses_client_runtime: '現在の設定では、ブラウザー内蔵の音声認識を使用します。',
  capture_capacity_exceeded: '音声録音の接続が上限に達しています。しばらくしてからもう一度お試しください。',
  final_transcription_failed: '文字起こしを完了できませんでした。もう一度お試しください。',
  forbidden: 'このアカウントには、この操作を実行する権限がありません。',
  llm_error: 'モデルサービスでリクエストを完了できませんでした。しばらくしてからもう一度お試しください。',
  no_speech_detected: '明瞭な音声を検出できませんでした。もう一度録音してください。',
  not_found: '必要な内容が見つかりません。更新または削除された可能性があります。',
  parser_error: 'アップロードした内容を解析できませんでした。ファイルを確認して、もう一度お試しください。',
  preview_capacity_exceeded: 'リアルタイム音声プレビューが混み合っています。録音全体は継続しています。',
  preview_unavailable: 'リアルタイム音声プレビューは現在利用できません。録音全体は継続しています。',
  realtime_failed: '音声録音または文字起こしに失敗しました。もう一度お試しください。',
  recording_storage_failed: '録音を保存できませんでした。しばらくしてからもう一度お試しください。',
  unauthorized: 'ログインの有効期限が切れました。もう一度ログインしてください。',
  workflow_error: '現在のプロセス段階では、この操作を実行できません。ページを更新して、もう一度お試しください。',
};

const ERROR_CODE_MESSAGES_BY_LANGUAGE: Readonly<Record<AppLanguage, Readonly<Record<string, string>>>> = {
  'zh-CN': ERROR_CODE_MESSAGES,
  en: ERROR_CODE_MESSAGES_ENGLISH,
  de: ERROR_CODE_MESSAGES_GERMAN,
  ja: ERROR_CODE_MESSAGES_JAPANESE,
};

const STATUS_ERROR_MESSAGES: Readonly<Record<number, string>> = {
  400: '提交的信息不正确，请检查后重试。',
  401: '登录状态已失效，请重新登录。',
  403: '当前账号没有执行此操作的权限。',
  404: '没有找到所需内容，它可能已被更新或删除。',
  409: '当前内容已发生变化，请刷新后重试。',
  413: '提交的内容过大，请缩小后重试。',
  422: '提交的信息不完整或格式不正确，请检查后重试。',
  429: '当前请求较多，请稍后重试。',
  500: '服务暂时出现异常，请稍后重试。',
  502: '模型服务暂时无法完成请求，请稍后重试。',
  503: '服务正在准备或暂不可用，请稍后重试。',
  504: '请求处理超时，请稍后重试。',
};

const STATUS_ERROR_MESSAGES_ENGLISH: Readonly<Record<number, string>> = {
  400: 'The submitted information is invalid. Check it and try again.',
  401: 'Your sign-in session has expired. Please sign in again.',
  403: 'This account does not have permission to perform this action.',
  404: 'The requested content could not be found. It may have been updated or removed.',
  409: 'This content changed while you were working. Refresh and try again.',
  413: 'The submitted content is too large. Reduce its size and try again.',
  422: 'The submitted information is incomplete or invalid. Check it and try again.',
  429: 'The service is receiving many requests. Please try again shortly.',
  500: 'The service encountered an error. Please try again later.',
  502: 'The model service could not complete the request. Please try again later.',
  503: 'The service is preparing or temporarily unavailable. Please try again later.',
  504: 'The request timed out. Please try again later.',
};

const STATUS_ERROR_MESSAGES_GERMAN: Readonly<Record<number, string>> = {
  400: 'Die übermittelten Angaben sind ungültig. Bitte prüfen Sie sie und versuchen Sie es erneut.',
  401: 'Ihre Anmeldung ist abgelaufen. Bitte melden Sie sich erneut an.',
  403: 'Dieses Konto ist für diese Aktion nicht berechtigt.',
  404: 'Der angeforderte Inhalt wurde nicht gefunden. Möglicherweise wurde er geändert oder entfernt.',
  409: 'Der Inhalt wurde zwischenzeitlich geändert. Bitte aktualisieren Sie die Seite und versuchen Sie es erneut.',
  413: 'Die übermittelten Daten sind zu groß. Bitte reduzieren Sie den Umfang und versuchen Sie es erneut.',
  422: 'Die übermittelten Angaben sind unvollständig oder ungültig. Bitte prüfen Sie sie und versuchen Sie es erneut.',
  429: 'Der Dienst verarbeitet derzeit viele Anfragen. Bitte versuchen Sie es in Kürze erneut.',
  500: 'Beim Dienst ist ein Fehler aufgetreten. Bitte versuchen Sie es später erneut.',
  502: 'Der Modelldienst konnte die Anfrage nicht abschließen. Bitte versuchen Sie es später erneut.',
  503: 'Der Dienst wird vorbereitet oder ist vorübergehend nicht verfügbar. Bitte versuchen Sie es später erneut.',
  504: 'Die Bearbeitung hat zu lange gedauert. Bitte versuchen Sie es erneut.',
};

const STATUS_ERROR_MESSAGES_JAPANESE: Readonly<Record<number, string>> = {
  400: '送信された情報が正しくありません。内容を確認して、もう一度お試しください。',
  401: 'ログインの有効期限が切れました。もう一度ログインしてください。',
  403: 'このアカウントには、この操作を実行する権限がありません。',
  404: '必要な内容が見つかりません。更新または削除された可能性があります。',
  409: '操作中に内容が変更されました。ページを更新して、もう一度お試しください。',
  413: '送信する内容が大きすぎます。サイズを小さくして、もう一度お試しください。',
  422: '送信された情報が不完全または不正です。内容を確認して、もう一度お試しください。',
  429: '現在リクエストが集中しています。しばらくしてからもう一度お試しください。',
  500: 'サービスでエラーが発生しました。しばらくしてからもう一度お試しください。',
  502: 'モデルサービスでリクエストを完了できませんでした。しばらくしてからもう一度お試しください。',
  503: 'サービスは準備中、または一時的に利用できません。しばらくしてからもう一度お試しください。',
  504: '処理がタイムアウトしました。もう一度お試しください。',
};

const STATUS_ERROR_MESSAGES_BY_LANGUAGE: Readonly<Record<AppLanguage, Readonly<Record<number, string>>>> = {
  'zh-CN': STATUS_ERROR_MESSAGES,
  en: STATUS_ERROR_MESSAGES_ENGLISH,
  de: STATUS_ERROR_MESSAGES_GERMAN,
  ja: STATUS_ERROR_MESSAGES_JAPANESE,
};

const GENERIC_REQUEST_ERROR: LocalizedText = {
  'zh-CN': '请求失败，请稍后重试。',
  en: 'The request failed. Please try again later.',
  de: 'Die Anfrage ist fehlgeschlagen. Bitte versuchen Sie es später erneut.',
  ja: 'リクエストに失敗しました。しばらくしてからもう一度お試しください。',
};

function statusErrorMessage(status?: number, fallback = '请求失败，请稍后重试。'): string {
  const language = getCurrentLanguage();
  const localizedStatus = status ? STATUS_ERROR_MESSAGES_BY_LANGUAGE[language][status] : undefined;
  if (localizedStatus) return localizedStatus;
  if (language === 'zh-CN') return fallback;
  if (language === 'en' && !/[\u3040-\u30ff\u3400-\u9fff]/u.test(fallback)) return fallback;
  return localizedText(GENERIC_REQUEST_ERROR, language);
}

function payloadErrorCandidate(payload: unknown): string | null {
  if (typeof payload === 'string') return payload;
  if (payload instanceof Error) return payload.message;
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const data = payload as Record<string, unknown>;
  for (const key of ['detail', 'message', 'error', 'raw'] as const) {
    const candidate = data[key];
    if (typeof candidate === 'string' && candidate.trim()) return candidate;
  }
  return null;
}

function looksLikeTechnicalError(value: string): boolean {
  if (!value) return true;
  if (/^[\[{]/.test(value) || /<\/?(?:html|body|pre)\b/i.test(value)) return true;
  if (/Traceback|ValidationError|input_value|stack trace|File\s+["'].*\.py|\/app\/|structured_output\.|schema_validation|malformed_json/i.test(value)) return true;
  if (/\b(?:TypeError|KeyError|ValueError|RuntimeError|AssertionError|HTTPStatusError)\b/.test(value)) return true;
  return false;
}

const DISPLAY_FRAGMENTS = {
  careerElementsApplyAnd: {
    'zh-CN': '当前适用 Career Elements，且',
    en: 'Career Elements apply, and ',
    de: 'Career Elements sind anwendbar und ',
    ja: 'Career Elements が適用され、',
  },
  whenCareerElementsDoNotApply: {
    'zh-CN': '当前不适用 Career Elements 时',
    en: 'when Career Elements do not apply',
    de: 'wenn Career Elements nicht anwendbar sind',
    ja: 'Career Elements が適用されない場合',
  },
  careerElementsApply: {
    'zh-CN': '当前适用 Career Elements',
    en: 'Career Elements apply',
    de: 'Career Elements sind anwendbar',
    ja: 'Career Elements が適用されます',
  },
  careerElementsDoNotApply: {
    'zh-CN': '当前不适用 Career Elements',
    en: 'Career Elements do not apply',
    de: 'Career Elements sind nicht anwendbar',
    ja: 'Career Elements は適用されません',
  },
  informationAvailable: {
    'zh-CN': '当前信息充分',
    en: 'information available',
    de: 'ausreichende Informationen vorhanden',
    ja: '情報は十分です',
  },
  insufficientInformation: {
    'zh-CN': '当前信息不足',
    en: 'insufficient information',
    de: 'unzureichende Informationen',
    ja: '情報が不足しています',
  },
  insufficientInformationShort: {
    'zh-CN': '信息不足',
    en: 'insufficient information',
    de: 'unzureichende Informationen',
    ja: '情報不足',
  },
} as const satisfies Readonly<Record<string, LocalizedText>>;

const ROLE_NAMES = {
  manager: {
    'zh-CN': '管理者',
    en: 'Manager',
    de: 'Führungskraft',
    ja: 'マネージャー',
  },
  employee: {
    'zh-CN': '员工',
    en: 'Employee',
    de: 'Mitarbeitende',
    ja: '従業員',
  },
} as const satisfies Readonly<Record<string, LocalizedText>>;

export function normalizeDisplayText(value: string): string {
  const language = getCurrentLanguage();
  let normalized = value
    .replace(/结果的突破型/g, '结果的突破性')
    .replace(
      /在\s*`?(?:context\.)?career_elements_applicable`?\s*[:=]\s*`?true`?\s*且/gi,
      localizedText(DISPLAY_FRAGMENTS.careerElementsApplyAnd, language),
    )
    .replace(
      /在\s*`?(?:context\.)?career_elements_applicable`?\s*[:=]\s*`?false`?\s*时/gi,
      localizedText(DISPLAY_FRAGMENTS.whenCareerElementsDoNotApply, language),
    )
    .replace(
      /`?(?:context\.)?career_elements_applicable`?\s*[:=]\s*`?true`?/gi,
      localizedText(DISPLAY_FRAGMENTS.careerElementsApply, language),
    )
    .replace(
      /`?(?:context\.)?career_elements_applicable`?\s*[:=]\s*`?false`?/gi,
      localizedText(DISPLAY_FRAGMENTS.careerElementsDoNotApply, language),
    )
    .replace(/\bstatus\s*[:=]\s*success\b/gi, localizedText(DISPLAY_FRAGMENTS.informationAvailable, language))
    .replace(/\bstatus\s*[:=]\s*insufficient_information\b/gi, localizedText(DISPLAY_FRAGMENTS.insufficientInformation, language))
    .replace(/\binsufficient_information\b/gi, localizedText(DISPLAY_FRAGMENTS.insufficientInformationShort, language));

  for (const [pattern, replacements] of INTERNAL_FIELD_REPLACEMENTS) {
    normalized = normalized.replace(pattern, localizedText(replacements, language));
  }
  return normalized
    .replace(/\bManager\b/g, localizedText(ROLE_NAMES.manager, language))
    .replace(/\bEmployee\b/g, localizedText(ROLE_NAMES.employee, language));
}

export function userFacingErrorMessage(
  payload: unknown,
  status?: number,
  fallback = '请求失败，请稍后重试。',
): string {
  const language = getCurrentLanguage();
  const candidate = payloadErrorCandidate(payload)?.trim() || '';
  const exactCodeMessage = ERROR_CODE_MESSAGES_BY_LANGUAGE[language][candidate.toLowerCase()];
  if (exactCodeMessage) return exactCodeMessage;
  if (!candidate || looksLikeTechnicalError(candidate)) {
    return statusErrorMessage(status, fallback);
  }

  if (/unknown\s+(?:primary_motive_id|secondary_motive_ids?)/i.test(candidate)) {
    return localizedText({
      'zh-CN': '员工诉求设置无效，请重新选择。',
      en: 'The employee motive selection is invalid. Select it again.',
      de: 'Die Auswahl der Mitarbeitermotive ist ungültig. Bitte wählen Sie erneut.',
      ja: '従業員の動機の選択が無効です。もう一度選択してください。',
    }, language);
  }
  if (/secondary_motive_ids|primary_motive_id/i.test(candidate)) {
    return localizedText({
      'zh-CN': '员工诉求设置不完整或存在冲突，请重新选择。',
      en: 'The employee motive selection is incomplete or conflicting. Select it again.',
      de: 'Die Auswahl der Mitarbeitermotive ist unvollständig oder widersprüchlich. Bitte wählen Sie erneut.',
      ja: '従業員の動機の選択が不完全、または矛盾しています。もう一度選択してください。',
    }, language);
  }
  if (/unknown\s+intent_id/i.test(candidate)) {
    return localizedText({
      'zh-CN': '所选沟通意图无效，请重新选择。',
      en: 'The selected conversation intent is invalid. Select it again.',
      de: 'Die ausgewählte Gesprächsabsicht ist ungültig. Bitte wählen Sie erneut.',
      ja: '選択した対話意図が無効です。もう一度選択してください。',
    }, language);
  }
  if (/field required|input should be|cannot be empty/i.test(candidate)) {
    return statusErrorMessage(status || 422, fallback);
  }

  const normalized = normalizeDisplayText(candidate);
  if ((language === 'en' || language === 'de') && /[\u3040-\u30ff\u3400-\u9fff]/u.test(normalized)) {
    return statusErrorMessage(status, fallback);
  }
  if (
    language === 'ja'
    && /[\u3400-\u9fff]/u.test(normalized)
    && !/[\u3040-\u30ff]/u.test(normalized)
  ) {
    return statusErrorMessage(status, fallback);
  }
  if (!/[\u3400-\u9fff]/u.test(normalized) && /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/i.test(normalized)) {
    return statusErrorMessage(status, fallback);
  }
  return normalized;
}
