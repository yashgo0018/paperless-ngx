import { DatePipe } from '@angular/common'
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http'
import { provideHttpClientTesting } from '@angular/common/http/testing'
import { ComponentFixture, TestBed } from '@angular/core/testing'
import { NgxBootstrapIconsModule, allIcons } from 'ngx-bootstrap-icons'
import { of, throwError } from 'rxjs'
import {
  DocumentTranslation,
  DocumentTranslationStatus,
} from 'src/app/data/document-translation'
import { DocumentService } from 'src/app/services/rest/document.service'
import { ToastService } from 'src/app/services/toast.service'
import {
  DocumentTranslationComponent,
  TRANSLATION_POLL_INTERVAL,
} from './document-translation.component'

const notTranslated: DocumentTranslation = {
  language: 'en-us',
  language_name: 'English (US)',
  status: null,
}

const done: DocumentTranslation = {
  ...notTranslated,
  status: DocumentTranslationStatus.Done,
  source_language: 'de',
  source_language_name: 'German',
  content: 'Dear Sir or Madam,\n\nwe confirm your registration.',
  model: 'qwen3.5:4b',
  modified: new Date(),
  outdated: false,
  truncated: false,
}

describe('DocumentTranslationComponent', () => {
  let component: DocumentTranslationComponent
  let fixture: ComponentFixture<DocumentTranslationComponent>
  let documentService: DocumentService
  let toastService: ToastService

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [
        DocumentTranslationComponent,
        NgxBootstrapIconsModule.pick(allIcons),
      ],
      providers: [
        DatePipe,
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
      ],
    }).compileComponents()

    fixture = TestBed.createComponent(DocumentTranslationComponent)
    documentService = TestBed.inject(DocumentService)
    toastService = TestBed.inject(ToastService)
    component = fixture.componentInstance
    fixture.componentRef.setInput('canEdit', true)
  })

  function init(translation: DocumentTranslation) {
    jest
      .spyOn(documentService, 'getTranslation')
      .mockReturnValue(of(translation))
    component.documentId = 1
    fixture.detectChanges()
  }

  it('should offer to translate a document not translated yet', () => {
    init(notTranslated)
    expect(fixture.nativeElement.textContent).toContain(
      'Translate into English (US)'
    )

    const requestSpy = jest
      .spyOn(documentService, 'requestTranslation')
      .mockReturnValue(of({ ...done }))
    fixture.nativeElement.querySelector('button.btn-primary').click()
    fixture.detectChanges()

    expect(requestSpy).toHaveBeenCalledWith(1)
    expect(fixture.nativeElement.textContent).toContain(
      'Translated from German into English (US)'
    )
  })

  it('should show a translation with its details', () => {
    init({ ...done, outdated: true, truncated: true })
    const text = fixture.nativeElement.textContent

    expect(text).toContain('we confirm your registration')
    expect(text).toContain('by qwen3.5:4b')
    expect(text).toContain('Outdated')
    expect(text).toContain('only its beginning was translated')
  })

  it('should say when a document is already in the language', () => {
    init({ ...done, source_language: 'en', source_language_name: 'English' })

    expect(component.alreadyInLanguage).toBe(true)
    expect(fixture.nativeElement.textContent).toContain(
      'already in English (US)'
    )
  })

  it('should check on a pending translation until it is done', () => {
    jest.useFakeTimers()
    const getSpy = jest
      .spyOn(documentService, 'getTranslation')
      .mockReturnValueOnce(
        of({ ...notTranslated, status: DocumentTranslationStatus.Pending })
      )
      .mockReturnValueOnce(of(done))
    component.documentId = 1
    fixture.detectChanges()
    expect(fixture.nativeElement.textContent).toContain('Translating into')

    jest.advanceTimersByTime(TRANSLATION_POLL_INTERVAL)
    fixture.detectChanges()

    expect(getSpy).toHaveBeenCalledTimes(2)
    expect(component.translation().status).toBe(DocumentTranslationStatus.Done)
    // no further polling once done
    jest.advanceTimersByTime(TRANSLATION_POLL_INTERVAL)
    expect(getSpy).toHaveBeenCalledTimes(2)
    jest.useRealTimers()
  })

  it('should stop polling when destroyed', () => {
    jest.useFakeTimers()
    const getSpy = jest
      .spyOn(documentService, 'getTranslation')
      .mockReturnValue(
        of({ ...notTranslated, status: DocumentTranslationStatus.Pending })
      )
    component.documentId = 1
    component.ngOnDestroy()

    jest.advanceTimersByTime(TRANSLATION_POLL_INTERVAL)

    expect(getSpy).toHaveBeenCalledTimes(1)
    jest.useRealTimers()
  })

  it('should show a failure and allow retrying', () => {
    init({
      ...notTranslated,
      status: DocumentTranslationStatus.Failed,
      error: 'The AI backend timed out.',
    })

    expect(fixture.nativeElement.textContent).toContain(
      'The AI backend timed out.'
    )
    expect(fixture.nativeElement.textContent).toContain('Try again')
  })

  it('should delete a translation', () => {
    init(done)
    const deleteSpy = jest
      .spyOn(documentService, 'deleteTranslation')
      .mockReturnValue(of(void 0))

    component.delete()
    fixture.detectChanges()

    expect(deleteSpy).toHaveBeenCalledWith(1)
    expect(component.translation()).toEqual(notTranslated)
  })

  it('should not allow translating without edit permissions', () => {
    fixture.componentRef.setInput('canEdit', false)
    init(notTranslated)

    expect(
      fixture.nativeElement.querySelector('button.btn-primary').disabled
    ).toBe(true)
  })

  it('should show errors', () => {
    const toastSpy = jest.spyOn(toastService, 'showError')
    jest
      .spyOn(documentService, 'getTranslation')
      .mockReturnValue(throwError(() => new Error('failed')))
    component.documentId = 1
    expect(toastSpy).toHaveBeenCalledTimes(1)

    jest.spyOn(documentService, 'getTranslation').mockReturnValue(of(done))
    component.documentId = 2
    jest
      .spyOn(documentService, 'requestTranslation')
      .mockReturnValue(throwError(() => new Error('failed')))
    component.translate()
    jest
      .spyOn(documentService, 'deleteTranslation')
      .mockReturnValue(throwError(() => new Error('failed')))
    component.delete()

    expect(toastSpy).toHaveBeenCalledTimes(3)
    expect(component.working()).toBe(false)
  })
})
