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
testCreateValidation()
console.log('Model.js event tests passed')
