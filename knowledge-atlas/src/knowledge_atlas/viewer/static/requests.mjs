// One request identity per deliberate decision; uncertain responses retain it.
export class MutationQueue {
  constructor(send,uuid=()=>crypto.randomUUID()){this.send=send;this.uuid=uuid;this.pending=null;}
  async begin(action,payload,publication){
    if(this.pending)throw new Error('Reconcile the pending operation before another decision.');
    this.pending={action,body:{payload:structuredClone(payload),request_id:this.uuid(),expected_publication:publication}};
    return this.reconcile();
  }
  async reconcile(){
    if(!this.pending)throw new Error('No pending request.');
    const p=this.pending;
    if(p.operation_id)return this.send('operation.read',{payload:{operation_id:p.operation_id}});
    const result=await this.send(p.action,p.body);
    if(result.operation_id)p.operation_id=result.operation_id;
    return result;
  }
  finish(){this.pending=null;}
}
