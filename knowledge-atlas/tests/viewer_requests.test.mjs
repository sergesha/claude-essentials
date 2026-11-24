import {test} from 'node:test';
import assert from 'node:assert/strict';
import {MutationQueue} from '../src/knowledge_atlas/viewer/static/requests.mjs';

test('lost acknowledgement retains exact intent and blocks a second decision',async()=>{
  let calls=[];
  const queue=new MutationQueue(async(action,body)=>{
    calls.push({action,body:structuredClone(body)});
    if(calls.length===1)throw new Error('connection lost');
    return {operation_id:'existing-operation',state:'running'};
  },()=> 'fixed-request-uuid');
  await assert.rejects(queue.begin('atlas','candidate.decide',{action:'accept',candidate:'first'},'base'));
  await assert.rejects(queue.begin('atlas','candidate.decide',{action:'accept',candidate:'second'},'base'));
  const result=await queue.reconcile('atlas');
  assert.equal(result.operation_id,'existing-operation');
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(queue.get('atlas').operation_id,'existing-operation');
  queue.finish('atlas');
  assert.equal(queue.get('atlas'),undefined);
});

test('pending operations are isolated by project and preserve identity on retry',async()=>{
 const calls=[];
 const queue=new MutationQueue(async(action,body)=>{
  calls.push({action,body:structuredClone(body)});
  if(calls.length===1)throw Error('lost acknowledgement');
  return {operation_id:body.atlas_id==='project-a'?'operation-a':'operation-b',state:'running'};
 },()=> 'request-id');
 await assert.rejects(queue.begin('project-a','candidate.decide',{candidate:'A'},'ready-a'),/lost acknowledgement/);
 await queue.begin('project-b','candidate.decide',{candidate:'B'},'ready-b');
 assert.equal(queue.get('project-b').operation_id,'operation-b');
 assert.equal(queue.get('project-a').body.atlas_id,'project-a');
 assert.equal((await queue.reconcile('project-a')).operation_id,'operation-a');
 assert.deepEqual(calls[0],calls[2]);
 assert.equal(calls[0].body.expected_publication,'ready-a');
 assert.equal(calls[1].body.expected_publication,'ready-b');
 queue.finish('project-b');
 assert.equal(queue.get('project-a').operation_id,'operation-a');
 assert.equal(queue.get('project-b'),undefined);
});

test('a busy submission replay retains its original request for another recovery',async()=>{
 const calls=[];
 const queue=new MutationQueue(async(action,body)=>{
  calls.push({action,body:structuredClone(body)});
  if(calls.length===1)throw Error('Connection lost');
  if(calls.length===2)throw Object.assign(Error('Busy'),{code:'busy',status:409});
  return {operation_id:'existing-operation',state:'running'};
 },()=> 'original-request');
 await assert.rejects(queue.begin('atlas','candidate.decide',{candidate:'A'},'base'),/Connection lost/);
 await assert.rejects(queue.reconcile('atlas'),/Busy/);
 assert.equal((await queue.reconcile('atlas')).operation_id,'existing-operation');
 assert.deepEqual(calls[1],calls[0]);
 assert.deepEqual(calls[2],calls[0]);
});

test('a polling rejection preserves the admitted operation for recovery',async()=>{
 const polls=[];
 const queue=new MutationQueue(async(action,body)=>{
  if(action==='operation.read'){
   polls.push(body);
   if(polls.length===1)throw Object.assign(Error('Operation not found'),{code:'operation_not_found',status:404});
  }
  return {operation_id:'admitted-operation',state:'running'};
 },()=> 'original-request');
 await queue.begin('atlas','candidate.decide',{candidate:'A'},'base');
 await assert.rejects(queue.reconcile('atlas'),/Operation not found/);
 assert.equal((await queue.reconcile('atlas')).operation_id,'admitted-operation');
 assert.deepEqual(polls,[
  {atlas_id:'atlas',payload:{operation_id:'admitted-operation'}},
  {atlas_id:'atlas',payload:{operation_id:'admitted-operation'}},
 ]);
});
