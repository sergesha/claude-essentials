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
  await assert.rejects(queue.begin('candidate.decide',{action:'accept',candidate:'first'},'base'));
  await assert.rejects(queue.begin('candidate.decide',{action:'accept',candidate:'second'},'base'));
  const result=await queue.reconcile();
  assert.equal(result.operation_id,'existing-operation');
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(queue.pending.operation_id,'existing-operation');
  queue.finish();
  assert.equal(queue.pending,null);
});
