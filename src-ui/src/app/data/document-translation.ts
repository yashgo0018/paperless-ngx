export enum DocumentTranslationStatus {
  Pending = 'pending',
  Done = 'done',
  Failed = 'failed',
}

export interface DocumentTranslation {
  language: string
  language_name: string
  // null when the document has not been translated into language yet
  status: DocumentTranslationStatus | null
  source_language?: string
  source_language_name?: string
  content?: string
  error?: string
  truncated?: boolean
  model?: string
  created?: Date
  modified?: Date
  // the document's text changed since it was translated
  outdated?: boolean
}
