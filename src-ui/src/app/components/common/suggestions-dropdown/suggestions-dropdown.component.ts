import {
  Component,
  EventEmitter,
  Output,
  ViewChild,
  input,
} from '@angular/core'
import { NgbDropdown, NgbDropdownModule } from '@ng-bootstrap/ng-bootstrap'
import { NgxBootstrapIconsModule } from 'ngx-bootstrap-icons'
import { CustomField, CustomFieldDataType } from 'src/app/data/custom-field'
import {
  CustomFieldSuggestion,
  DocumentSuggestions,
} from 'src/app/data/document-suggestions'
import { SuggestionSource } from 'src/app/data/ui-settings'
import { pngxPopperOptions } from 'src/app/utils/popper-options'

@Component({
  selector: 'pngx-suggestions-dropdown',
  imports: [NgbDropdownModule, NgxBootstrapIconsModule],
  templateUrl: './suggestions-dropdown.component.html',
  styleUrl: './suggestions-dropdown.component.scss',
})
export class SuggestionsDropdownComponent {
  public popperOptions = pngxPopperOptions
  public readonly SuggestionSource = SuggestionSource

  @ViewChild('dropdown') dropdown: NgbDropdown
  readonly suggestions = input<DocumentSuggestions>(null)
  readonly aiEnabled = input(false)
  readonly loading = input(false)
  readonly disabled = input(false)
  readonly source = input<SuggestionSource>(SuggestionSource.ML)
  readonly defaultSource = input<SuggestionSource>(SuggestionSource.ML)
  readonly fetchedSources = input<SuggestionSource[]>([])

  readonly appliedTags = input<number[]>([])
  readonly appliedCorrespondent = input<number>(null)
  readonly appliedDocumentType = input<number>(null)
  readonly appliedStoragePath = input<number>(null)
  readonly appliedTitle = input<string>(null)
  readonly appliedCreated = input<string>(null)
  readonly customFields = input<CustomField[]>([])
  readonly appliedCustomFields = input<{ field: number; value: any }[]>([])

  @Output()
  getSuggestions: EventEmitter<SuggestionSource> = new EventEmitter()

  @Output()
  sourceChange: EventEmitter<SuggestionSource> = new EventEmitter()

  @Output()
  addTag: EventEmitter<string> = new EventEmitter()

  @Output()
  addDocumentType: EventEmitter<string> = new EventEmitter()

  @Output()
  addCorrespondent: EventEmitter<string> = new EventEmitter()

  @Output()
  applyCustomField: EventEmitter<CustomFieldSuggestion> = new EventEmitter()

  public clickSuggest(): void {
    if (this.disabled() || this.loading()) {
      return
    }

    if (!this.suggestions()) {
      this.getSuggestions.emit(this.source())
    } else if (this.hasUnfetchedSources) {
      // sources changed, fetch the rest and show what we have meanwhile
      this.getSuggestions.emit(this.source())
      this.dropdown?.open()
    } else {
      this.dropdown?.toggle()
    }
  }

  get useML(): boolean {
    return this.source() !== SuggestionSource.AI
  }

  get useAI(): boolean {
    return this.source() !== SuggestionSource.ML
  }

  get hasUnfetchedSources(): boolean {
    const fetched = this.fetchedSources()
    return (
      (this.useML && !fetched.includes(SuggestionSource.ML)) ||
      (this.useAI && !fetched.includes(SuggestionSource.AI))
    )
  }

  public setSources(ml: boolean, ai: boolean) {
    if (ml && ai) {
      this.sourceChange.emit(SuggestionSource.Both)
    } else if (ml) {
      this.sourceChange.emit(SuggestionSource.ML)
    } else if (ai) {
      this.sourceChange.emit(SuggestionSource.AI)
    }
  }

  get novelSuggestions(): number {
    return (
      (this.suggestions()?.suggested_correspondents?.length ?? 0) +
      (this.suggestions()?.suggested_tags?.length ?? 0) +
      (this.suggestions()?.suggested_document_types?.length ?? 0) +
      this.customFieldSuggestions.length
    )
  }

  // suggested values of known custom fields, unless the document has them
  get customFieldSuggestions(): CustomFieldSuggestion[] {
    return (this.suggestions()?.custom_fields ?? []).filter(
      (suggestion) =>
        this.getCustomField(suggestion) &&
        !(this.appliedCustomFields() ?? []).some(
          (applied) =>
            applied.field === suggestion.field &&
            applied.value === suggestion.value
        )
    )
  }

  public getCustomField(suggestion: CustomFieldSuggestion): CustomField {
    return this.customFields()?.find((field) => field.id === suggestion.field)
  }

  public customFieldDisplayValue(suggestion: CustomFieldSuggestion): string {
    const field = this.getCustomField(suggestion)
    switch (field?.data_type) {
      case CustomFieldDataType.Select:
        return (
          field.extra_data?.select_options?.find(
            (option) => option.id === suggestion.value
          )?.label ?? suggestion.value
        )
      case CustomFieldDataType.Boolean:
        return suggestion.value ? $localize`Yes` : $localize`No`
      default:
        return String(suggestion.value)
    }
  }

  get reusableSuggestions(): number {
    const correspondent = this.appliedCorrespondent()
    const documentType = this.appliedDocumentType()
    const storagePath = this.appliedStoragePath()
    // Storage paths count here but not in novelSuggestions: an existing one
    // can be applied from the field, a suggested name cannot create one.
    return (
      this.countUnapplied(this.suggestions()?.tags, this.appliedTags()) +
      this.countUnapplied(
        this.suggestions()?.correspondents,
        correspondent ? [correspondent] : []
      ) +
      this.countUnapplied(
        this.suggestions()?.document_types,
        documentType ? [documentType] : []
      ) +
      this.countUnapplied(
        this.suggestions()?.storage_paths,
        storagePath ? [storagePath] : []
      )
    )
  }

  get totalSuggestions(): number {
    return this.novelSuggestions + this.fieldSuggestions
  }

  get fieldSuggestions(): number {
    return (
      this.reusableSuggestions +
      this.unappliedTitleSuggestions +
      this.unappliedDateSuggestions
    )
  }

  // hide a title or date suggestion equal to the current value
  private get unappliedTitleSuggestions(): number {
    const title = this.suggestions()?.title
    return title && title !== this.appliedTitle() ? 1 : 0
  }

  private get unappliedDateSuggestions(): number {
    const created = this.appliedCreated()
    return (this.suggestions()?.dates ?? []).filter(
      (date) => !created || date !== created
    ).length
  }

  private countUnapplied(suggested: number[], applied: number[]): number {
    return (suggested ?? []).filter((id) => !(applied ?? []).includes(id))
      .length
  }

  get noSuggestions(): boolean {
    const suggestions = this.suggestions()
    return (
      !this.loading() &&
      suggestions != null &&
      !suggestions.title &&
      !suggestions.tags?.length &&
      !suggestions.suggested_tags?.length &&
      !suggestions.correspondents?.length &&
      !suggestions.suggested_correspondents?.length &&
      !suggestions.document_types?.length &&
      !suggestions.suggested_document_types?.length &&
      !suggestions.storage_paths?.length &&
      !suggestions.suggested_storage_paths?.length &&
      !suggestions.dates?.length &&
      !suggestions.custom_fields?.length
    )
  }
}
