// One request identity per deliberate decision; uncertain responses retain it.
export class MutationQueue {
  constructor(send,uuid=()=>crypto.randomUUID()){
    this.send=send;
    this.uuid=uuid;
    this.projects=new Map();
  }

  get(atlasId){return this.projects.get(atlasId);}
  get hasPending(){return this.projects.size>0;}

  async begin(atlasId,action,payload,publication){
    if(this.get(atlasId))throw new Error('Reconcile the pending operation before another decision.');
    this.projects.set(atlasId,{action,body:{
      atlas_id:atlasId,payload:structuredClone(payload),request_id:this.uuid(),expected_publication:publication,
    }});
    return this.reconcile(atlasId);
  }

  async reconcile(atlasId){
    const pending=this.get(atlasId);
    if(!pending)throw new Error('No pending request.');
    if(pending.result)return pending.result;
    if(!pending.inFlight){
      const operationId=pending.operation_id;
      pending.inFlight=Promise.resolve().then(()=>operationId
        ?this.send('operation.read',{atlas_id:atlasId,payload:{operation_id:operationId}})
        :this.send(pending.action,pending.body)
      ).then(result=>{
        if(result.operation_id)pending.operation_id=result.operation_id;
        if(!['queued','running'].includes(result.state))pending.result=result;
        return result;
      }).catch(error=>{
        // Polling failures leave admitted work available for recovery.
        const rejected=error.status>=400&&error.status<500&&error.code!=='busy';
        if(!operationId&&rejected&&this.get(atlasId)===pending)this.finish(atlasId);
        throw error;
      }).finally(()=>{pending.inFlight=null;});
    }
    return pending.inFlight;
  }

  finish(atlasId){this.projects.delete(atlasId);}
}
