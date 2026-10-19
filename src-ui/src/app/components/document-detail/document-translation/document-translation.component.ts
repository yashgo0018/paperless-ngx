import {
  Component,
  Input,
  OnDestroy,
  inject,
  input,
  signal,
} from '@angular/core'
import { NgxBootstrapIconsModule } from 'ngx-bootstrap-icons'
import { Subscription, first, timer } from 'rxjs'
import {
  DocumentTranslation,
  DocumentTranslationStatus,
} from 'src/app/data/document-translation'
import { CustomDatePipe } from 'src/app/pipes/custom-date.pipe'
import { DocumentService } from 'src/app/services/rest/document.service'
import { ToastService } from 'src/app/services/toast.service'

// how often to check on a translation in progress, in ms
export const TRANSLATION_POLL_INTERVAL = 5000

@Component({
  selector: 'pngx-document-translation',
  templateUrl: './document-translation.component.html',
  styleUrl: './document-translation.component.scss',
  imports: [CustomDatePipe, NgxBootstrapIconsModule],
})
export class DocumentTranslationComponent implements OnDestroy {
  private readonly documentService = inject(DocumentService)
  private readonly toastService = inject(ToastService)

  public readonly DocumentTranslationStatus = DocumentTranslationStatus

  readonly canEdit = input(false)
  readonly loading = signal(true)
  readonly working = signal(false)
  readonly translation = signal<DocumentTranslation>(undefined)

  private _documentId: number
  private poll: Subscription

  @Input()
  set documentId(id: number) {
    if (this._documentId !== id) {
      this._documentId = id
      this.loading.set(true)
      this.load()
    }
  }

  get alreadyInLanguage(): boolean {
    const translation = this.translation()
    const source = translation?.source_language?.split('-')[0]
    return (
      translation?.status === DocumentTranslationStatus.Done &&
      !!source &&
      translation.language.split('-')[0] === source
    )
  }

  public translate(): void {
    this.working.set(true)
    this.documentService
      .requestTranslation(this._documentId)
      .pipe(first())
      .subscribe({
        next: (translation) => {
          this.working.set(false)
          this.update(translation)
        },
        error: (error) => {
          this.working.set(false)
          this.toastService.showError(
            $localize`Error requesting translation`,
            error
          )
        },
      })
  }

  public delete(): void {
    const { language, language_name } = this.translation()
    this.working.set(true)
    this.documentService
      .deleteTranslation(this._documentId)
      .pipe(first())
      .subscribe({
        next: () => {
          this.working.set(false)
          this.update({ language, language_name, status: null })
        },
        error: (error) => {
          this.working.set(false)
          this.toastService.showError(
            $localize`Error deleting translation`,
            error
          )
        },
      })
  }

  ngOnDestroy(): void {
    this.poll?.unsubscribe()
  }

  private load(): void {
    this.poll?.unsubscribe()
    if (!this._documentId) return
    this.documentService
      .getTranslation(this._documentId)
      .pipe(first())
      .subscribe({
        next: (translation) => {
          this.loading.set(false)
          this.update(translation)
        },
        error: (error) => {
          this.loading.set(false)
          this.toastService.showError(
            $localize`Error retrieving translation`,
            error
          )
        },
      })
  }

  private update(translation: DocumentTranslation): void {
    this.translation.set(translation)
    this.poll?.unsubscribe()
    if (translation?.status === DocumentTranslationStatus.Pending) {
      this.poll = timer(TRANSLATION_POLL_INTERVAL).subscribe(() => this.load())
    }
  }
}
