'use strict'

const assert = require('node:assert/strict')
const Model = require('../Model.js')

function testEventNormalization() {
  const events = Model.normalizeEvents([
    { start: '2026-08-19T14:00:00Z', end: '2026-08-19T15:30:00Z', title: '  Design review  ', calendarId: 'work', calendarName: 'Work', allDay: false, color: '#88aaff' },
    { start: 'not-a-date', end: 'also-bad', title: 'Bad', calendarId: 'work' }
  ])
  assert.equal(events.length, 1)
  assert.equal(events[0].title, 'Design review')
  assert.equal(events[0].calendarName, 'Work')
}

function testDateMapping() {
  const mapped = Model.mapEventsByDate([
    { start: '2026-08-19', end: '2026-08-22', title: 'Trip', calendarId: 'home', calendarName: 'Home', allDay: true, color: '#112233' },
    { start: '2026-08-20T23:30:00', end: '2026-08-21T00:30:00', title: 'Deploy', calendarId: 'work', calendarName: 'Work', allDay: false, color: '#445566' }
  ])
  assert.deepEqual(Object.keys(mapped).sort(), ['2026-08-19', '2026-08-20', '2026-08-21'])
  assert.equal(mapped['2026-08-22'], undefined)
}

function testSubscriptionValidation() {
  const checked = Model.validateSubscriptionInput({
    name: ' Personal ', url: 'https://calendar.example/private.ics',
    username: 'me', password: 'app-password', color: '#5E81AC'
  })
  assert.equal(checked.valid, true)
  assert.deepEqual(checked.value, {
    action: 'add', name: 'Personal', url: 'https://calendar.example/private.ics',
    username: 'me', password: 'app-password', color: '#5e81ac'
  })
  assert.equal(Model.validateSubscriptionInput({ name: '', url: 'https://example.com/a.ics' }).field, 'name')
  assert.equal(Model.validateSubscriptionInput({ name: 'A', url: 'http://example.com/a.ics' }).field, 'url')
  assert.equal(Model.validateSubscriptionInput({ name: 'A', url: 'https://me:x@example.com/a.ics' }).field, 'url')
  assert.equal(Model.validateSubscriptionInput({ name: 'A', url: 'https://example.com/a.ics#secret' }).field, 'url')
  assert.equal(Model.validateSubscriptionInput({ name: 'A', url: 'https://example.com/a.ics', username: 'me' }).field, 'password')
  assert.equal(Model.validateSubscriptionInput({ name: 'A', url: 'https://example.com/a.ics', color: '#12345g' }).field, 'color')
}

function testDurableMetadataOmitsSecrets() {
  const metadata = Model.durableSubscriptionMetadata({
    id: '0123456789abcdef0123456789abcdef', name: 'Work', color: '#AABBCC',
    url: 'https://example.com/private.ics', username: 'me', password: 'never-store'
  })
  assert.deepEqual(metadata, { id: '0123456789abcdef0123456789abcdef', name: 'Work', color: '#aabbcc' })
  assert.equal(JSON.stringify(metadata).includes('private.ics'), false)
  assert.equal(JSON.stringify(metadata).includes('never-store'), false)
}

function testNdjsonOrdering() {
  const state = { finalSeen: false, progressCount: 0 }
  const progress = Model.parseSubscriptionProtocolLine(JSON.stringify({
    type: 'progress', final: false, requestId: 'sub-1', stage: 'fetching'
  }), 'sub-1', state)
  assert.equal(progress.valid, true)
  assert.equal(progress.kind, 'progress')
  const final = Model.parseSubscriptionProtocolLine(JSON.stringify({
    type: 'result', final: true, ok: true, requestId: 'sub-1'
  }), 'sub-1', progress.state)
  assert.equal(final.valid, true)
  assert.equal(Model.parseSubscriptionProtocolLine(JSON.stringify({
    type: 'progress', final: false, requestId: 'sub-1', stage: 'late'
  }), 'sub-1', final.state).valid, false)
  assert.equal(Model.parseSubscriptionProtocolLine(JSON.stringify({
    type: 'result', final: true, ok: true, requestId: 'wrong'
  }), 'sub-1', state).valid, false)
}

function testStatusNormalization() {
  const status = Model.normalizeSubscriptionStatus([
    { id: 'a'.repeat(32), name: 'Personal', color: '#123456', url: 'secret' },
    { id: 'b'.repeat(32), name: 'Work' }
  ], { subscriptions: [
    { id: 'a'.repeat(32), ok: true, events: 12 },
    { id: 'b'.repeat(32), ok: false, error: { code: 'fetch_failed', message: 'Feed unavailable' } }
  ] })
  assert.deepEqual(status[0], { id: 'a'.repeat(32), name: 'Personal', color: '#123456', status: 'ok', statusText: '12 events' })
  assert.equal(status[1].status, 'error')
  assert.equal(status[1].statusText, 'Feed unavailable')
  assert.equal(JSON.stringify(status).includes('secret'), false)
  assert.equal(Model.normalizeSubscriptionStatus([{ id: 'c'.repeat(32), name: 'New' }], null)[0].status, 'unknown')
}

testEventNormalization()
testDateMapping()
testSubscriptionValidation()
testDurableMetadataOmitsSecrets()
testNdjsonOrdering()
testStatusNormalization()
console.log('Model.js tests passed')
