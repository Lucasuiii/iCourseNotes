const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8');
const layout = new JSDOM(html);
const courseTemplate = [...layout.window.document.querySelectorAll('template')]
  .find(element => element.getAttribute('x-for') === 'c in courses');
const card = courseTemplate.content.querySelector('.course-card');
assert.equal(card.querySelectorAll('button').length, 3, 'Open, pin and delete are independent controls');
assert.equal(card.querySelectorAll('button button').length, 0);
assert.equal(card.querySelectorAll('button')[2].getAttribute(':aria-label'), "'删除课程：' + c.title");
const dialog = layout.window.document.querySelector('[aria-labelledby="course-delete-title"]');
assert.equal(dialog.getAttribute('role'), 'dialog');
assert.ok(dialog.textContent.includes('取消这门课的订阅'));
assert.ok(dialog.textContent.includes('所有课次'));
layout.window.close();

function fixture(saved = ['1', '2', '3']) {
  const dom = new JSDOM('<body></body>', {url: 'https://example.invalid/', runScripts: 'outside-only'});
  const w = dom.window;
  w.ICS = {db: {getMeta: () => saved.join(','), getCoursesByIds: ids => ids.map(course_id => ({course_id}))}, github: {}};
  let factory;
  w.Alpine = {data: (_, f) => {factory = f;}};
  w.eval(fs.readFileSync(path.join(root, 'frontend/js/app.js'), 'utf8'));
  w.document.dispatchEvent(new w.Event('alpine:init'));
  const app = factory();
  app.repoOwner = 'example'; app.repoName = 'notes';
  app.courses = [{course_id:'1',title:'甲课'}, {course_id:'2',title:'乙课'}];
  app.currentCourse = app.courses[0];
  app.subscribedIds = [...saved];
  app._toast = () => {};
  const requests = [];
  w.ICS.github.setCourseIdsSecret = async (...args) => {requests.push(['unsubscribe', Array.from(args[3])]);};
  w.ICS.github.triggerDeleteWorkflow = async (...args) => {requests.push(['delete', args[2], Array.from(args[4]), Array.from(args[5])]);};
  w.sessionStorage.setItem('ics_creds', JSON.stringify({token:'synthetic-test-token'}));
  return {app,w,dom,requests};
}

(async () => {
  let f = fixture();
  f.app.openCourseDeleteDialog({course_id:'unrelated',title:'非法目标'});
  assert.equal(f.app.courseDeleteTarget, null);
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  f.app.closeCourseDeleteDialog();
  await f.app.confirmCourseDelete();
  assert.equal(f.requests.length, 0, 'Cancel never writes anything');
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  f.w.sessionStorage.removeItem('ics_creds');
  await f.app.confirmCourseDelete();
  assert.match(f.app.courseDeleteError, /管理授权/);
  assert.equal(f.requests.length, 0, 'Key-only readers cannot delete');
  f.dom.window.close();

  f = fixture();
  f.app.subscribedIds = ['1','2','4']; // Unsaved subscription changes.
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  await f.app.confirmCourseDelete();
  assert.deepEqual(f.requests, [['unsubscribe',['1','3']], ['delete','main',['2'],[]]]);
  assert.deepEqual(Array.from(f.app.subscribedIds), ['1','4'], 'Do not discard other draft edits');
  assert.deepEqual(JSON.parse(f.w.sessionStorage.getItem('ics_lastSubscribed:example%2Fnotes')), ['1','3']);
  assert.equal(f.app.courses.length, 2, 'Dispatch success is not proof of completed deletion');
  assert.equal(f.app.courseDeleteTarget, null);
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  await f.app.confirmCourseDelete();
  assert.equal(f.requests.length, 2, 'No repeated dispatch');
  f.dom.window.close();

  f = fixture();
  f.w.ICS.github.setCourseIdsSecret = async () => {throw new Error('denied');};
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  await f.app.confirmCourseDelete();
  assert.match(f.app.courseDeleteError, /尚未提交删除/);
  assert.equal(f.requests.length, 0);
  assert.deepEqual(Array.from(f.app.subscribedIds), ['1','2','3']);
  assert.equal(f.app.courseDeleteRequested.length, 0);
  assert.equal(f.app.courseDeleteBusy, false);
  f.dom.window.close();

  f = fixture();
  f.w.ICS.github.triggerDeleteWorkflow = async (...args) => {
    f.requests.push(['delete',Array.from(args[4])]);
    throw new Error('response lost');
  };
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  await f.app.confirmCourseDelete();
  assert.match(f.app.courseDeleteError, /取消订阅已保存/);
  assert.match(f.app.courseDeleteError, /不会重复提交/);
  await f.app.confirmCourseDelete();
  assert.equal(f.requests.length, 2);
  assert.ok(f.app.courseDeleteTarget);
  f.dom.window.close();

  f = fixture(['2']);
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  await f.app.confirmCourseDelete();
  assert.deepEqual(f.requests, [['unsubscribe',[]], ['delete','main',['2'],[]]], 'Last subscription and zero-lecture courses are supported');
  f.dom.window.close();

  f = fixture(['1']);
  let release;
  f.w.ICS.github.triggerDeleteWorkflow = async (...args) => {
    f.requests.push(['delete',Array.from(args[4])]);
    await new Promise(resolve => {release = resolve;});
  };
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  const pending = f.app.confirmCourseDelete();
  await f.app.confirmCourseDelete();
  f.app.closeCourseDeleteDialog();
  f.app.openCourseDeleteDialog(f.app.courses[0]);
  await f.app.saveSubscriptions();
  assert.equal(f.app.courseDeleteTarget.course_id, '2', 'Busy target cannot change');
  assert.equal(f.requests.length, 1);
  release(); await pending;
  assert.equal(f.app.courseDeleteBusy, false);
  f.dom.window.close();

  f = fixture();
  f.w.ICS.db.getMeta = key => key === 'subscribed_course_ids' ? '' : '1,2,3';
  f.app.openCourseDeleteDialog(f.app.courses[1]);
  await f.app.confirmCourseDelete();
  assert.deepEqual(f.requests, [['delete','main',['2'],[]]], 'An authoritative empty subscription must not revive the legacy list');
  f.dom.window.close();
  console.log('Whole-course target, subscription preservation, auth, cancellation and ambiguous-dispatch checks passed.');
})().catch(error => {console.error(error);process.exitCode = 1;});
