'use strict'

const assert = require('node:assert/strict')
const Model = require('../Model.js')

function testEventNormalization() {
  const events = Model.normalizeEvents([
    {
      start: '2026-08-19T14:00:00Z',
      end: '2026-08-19T15:30:00Z',
      title: '  Design review  ',
      calendarId: 'work',
      calendarName: 'Work',
      allDay: false,
      color: '#88aaff'
    },
    { start: 'not-a-date', end: 'also-bad', title: 'Bad', calendarId: 'work' },
    null
  ])

  assert.equal(events.length, 1)
  assert.equal(events[0].title, 'Design review')
  assert.equal(events[0].calendarName, 'Work')
  assert.equal(events[0].color, '#88aaff')
  assert.equal(events[0].allDay, false)

  const fallback = Model.normalizeEvent({
    start: '2026-08-20', end: '2026-08-21', title: 'Holiday',
    calendarId: 'home', calendarName: '', allDay: true, color: 'red'
  })
  assert.equal(fallback.calendarName, 'home')
  assert.equal(fallback.color, '#7aa2f7')
}

function testDateMapping() {
  const mapped = Model.mapEventsByDate([
    {
      start: '2026-08-19', end: '2026-08-22', title: 'Trip',
      calendarId: 'home', calendarName: 'Home', allDay: true, color: '#112233'
    },
    {
      start: '2026-08-20T23:30:00', end: '2026-08-21T00:30:00', title: 'Deploy',
      calendarId: 'work', calendarName: 'Work', allDay: false, color: '#445566'
    }
  ])

  assert.deepEqual(Object.keys(mapped).sort(), [
    '2026-08-19', '2026-08-20', '2026-08-21'
  ])
  assert.equal(mapped['2026-08-19'].length, 1)
  assert.equal(mapped['2026-08-20'].length, 2)
  assert.equal(mapped['2026-08-21'].length, 2)
  assert.equal(mapped['2026-08-22'], undefined, 'all-day end is exclusive')
}

function testSetupValidation() {
  const google = Model.validateSetupInput({
    provider: 'google', displayName: ' Personal ',
    clientId: 'desktop.apps.googleusercontent.com', secret: 'oauth-secret'
  })
  assert.equal(google.valid, true)
  assert.deepEqual(google.value, {
    provider: 'google', displayName: 'Personal',
    clientId: 'desktop.apps.googleusercontent.com', secret: 'oauth-secret'
  })

  const icloud = Model.validateSetupInput({
    provider: 'icloud', username: 'me@icloud.com', secret: 'app-password'
  })
  assert.equal(icloud.valid, true)
  assert.equal(icloud.value.url, undefined)
  assert.equal(icloud.value.clientId, undefined)

  const caldav = Model.validateSetupInput({
    provider: 'caldav', username: 'me@example.com',
    url: 'https://calendar.example.com/dav/', secret: 'app-password'
  })
  assert.equal(caldav.valid, true)

  assert.equal(Model.validateSetupInput({ provider: 'other', secret: 'x' }).field, 'provider')
  assert.equal(Model.validateSetupInput({ provider: 'google', clientId: '', secret: 'x' }).field, 'clientId')
  assert.equal(Model.validateSetupInput({ provider: 'icloud', username: '', secret: 'x' }).field, 'username')
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'http://example.com', secret: 'x'
  }).field, 'url')
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'ftp://example.com', secret: 'x'
  }).field, 'url')
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'https://me:secret@example.com/dav', secret: 'x'
  }).field, 'url')
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'https://example.com/dav#calendar', secret: 'x'
  }).field, 'url')
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'https://example.com:bad/dav', secret: 'x'
  }).field, 'url')
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'https://calendar.lan:8443/dav', secret: 'x'
  }).valid, true)
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'https://[fd00::1]/dav', secret: 'x'
  }).valid, true)
  assert.equal(Model.validateSetupInput({
    provider: 'caldav', username: 'me', url: 'https://example.com/dav', secret: ''
  }).field, 'secret')
  assert.equal(Model.validateSetupInput({
    provider: 'google', clientId: 'x'.repeat(513), secret: 'x'
  }).field, 'clientId')
  assert.equal(Model.validateSetupInput({
    provider: 'icloud', username: 'me@icloud.com\nother', secret: 'x'
  }).field, 'username')

  const durable = Model.durableSetupFields({
    provider: 'caldav', displayName: 'Work', username: 'me',
    url: 'https://example.com/dav', secret: 'never-store',
    password: 'also-never-store', clientSecret: 'still-never-store'
  })
  assert.deepEqual(durable, {
    provider: 'caldav', displayName: 'Work', username: 'me', url: 'https://example.com/dav'
  })
  assert.equal(JSON.stringify(durable).includes('never-store'), false)
}

function testSetupProtocolLifecycle() {
  const state = { browserSeen: false, finalSeen: false }
  const malformed = Model.parseSetupProtocolLine('{bad', 'request-1', state)
  assert.equal(malformed.valid, false)

  const browserLine = JSON.stringify({
    type: 'browser', requestId: 'request-1',
    url: 'https://accounts.google.com/o/oauth2/auth?client_id=desktop'
  })
  const browser = Model.parseSetupProtocolLine(browserLine, 'request-1', state)
  assert.equal(browser.valid, true)
  assert.equal(browser.kind, 'browser')
  assert.equal(Model.parseSetupProtocolLine(browserLine, 'request-1', browser.state).valid, false)
  assert.equal(Model.parseSetupProtocolLine(JSON.stringify({
    type: 'browser', requestId: 'request-1', url: 'https://evil.example/auth'
  }), 'request-1', state).valid, false)

  const finalLine = JSON.stringify({ type: 'result', final: true, ok: true, requestId: 'request-1' })
  const final = Model.parseSetupProtocolLine(finalLine, 'request-1', browser.state)
  assert.equal(final.valid, true)
  assert.equal(Model.parseSetupProtocolLine(finalLine, 'request-1', final.state).valid, false)

  const committedAfterCancel = Model.setupResultPresentation({
    ok: true, replacesExisting: true, cleanupComplete: false
  }, true)
  assert.equal(committedAfterCancel.state, 'success')
  assert.equal(committedAfterCancel.message, 'Calendar account replaced successfully')
  assert.match(committedAfterCancel.warning, /old credential or token/)
  assert.equal(Model.setupResultPresentation(null, true).state, 'cancelled')
}

function testCreateValidation() {
  assert.deepEqual(Model.validateCreateInput({
    calendarId: 'work', title: ' Planning ', date: '2026-08-19', start: '09:05', end: '10:30'
  }), {
    valid: true,
    field: '',
    error: '',
    value: {
      calendarId: 'work', title: 'Planning',
      start: '2026-08-19T09:05', end: '2026-08-19T10:30', allDay: false, sync: false
    }
  })

  assert.equal(Model.validateCreateInput({
    calendarId: '', title: 'Planning', date: '2026-08-19', start: '09:00', end: '10:00'
  }).field, 'calendarId')
  assert.equal(Model.validateCreateInput({
    calendarId: 'work', title: '', date: '2026-08-19', start: '09:00', end: '10:00'
  }).field, 'title')
  assert.equal(Model.validateCreateInput({
    calendarId: 'work', title: 'Planning', date: '2026-02-30', start: '09:00', end: '10:00'
  }).field, 'date')
  assert.equal(Model.validateCreateInput({
    calendarId: 'work', title: 'Planning', date: '2026-08-19', start: '9:00', end: '10:00'
  }).field, 'start')
  assert.equal(Model.validateCreateInput({
    calendarId: 'work', title: 'Planning', date: '2026-08-19', start: '10:00', end: '10:00'
  }).field, 'end')
}

testEventNormalization()
testDateMapping()
testSetupValidation()
testSetupProtocolLifecycle()
testCreateValidation()
console.log('Model.js tests passed')
