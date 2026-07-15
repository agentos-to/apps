const __g = (window.__agmail = window.__agmail || {});
if (!__g.hooked) {
  __g.hooked = true; __g.bv=[]; __g.fd=[]; __g.actions=[]; __g.hdrs=null;
  const oOpen=XMLHttpRequest.prototype.open, oSend=XMLHttpRequest.prototype.send, oSet=XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open=function(m,u,...r){this.__u=u;this.__h={};return oOpen.call(this,m,u,...r)};
  XMLHttpRequest.prototype.setRequestHeader=function(k,v){try{this.__h[k]=v}catch(e){}return oSet.call(this,k,v)};
  XMLHttpRequest.prototype.send=function(b){
    const x=this,u=String(this.__u||"");
    if(u.indexOf("/i/bv")!==-1||u.indexOf("/i/fd")!==-1){
      const bucket=u.indexOf("/i/bv")!==-1?__g.bv:__g.fd;
      this.addEventListener("load",function(){try{bucket.push(x.responseText)}catch(e){}});
    }
    if(/\/i\/s(\?|$)/.test(u)){
      const rec={body:typeof b==="string"?b:"",ts:Date.now(),status:null}; __g.actions.push(rec);
      this.addEventListener("load",function(){try{rec.status=x.status}catch(e){}});
    }
    if(u.indexOf("/sync/")!==-1 && this.__h && this.__h["X-Framework-Xsrf-Token"]) __g.hdrs=this.__h;
    return oSend.call(this,b);
  };
}
__g.bv=[]; __g.fd=[];
location.hash = "#inbox";
await new Promise(r=>setTimeout(r,800));
location.hash = "#search/subject%3AREPLYVERIFY-J1";
await new Promise(r=>setTimeout(r,3500));

const hex="19f48a0360c4a115";
const tidA="thread-a:r-585664302285623793";
const tidF="thread-f:"+parseInt(hex,16);

function walkFind(node, path, acc, depth){
  if(depth>14||acc.length>80) return;
  if(typeof node==="string"){
    if(node.includes(hex)||node===tidA||node===tidF||node.startsWith("thread-")||node.startsWith("msg-")||node.startsWith("Ktbx"))
      acc.push({path, v:node.slice(0,100)});
    return;
  }
  if(Array.isArray(node)){
    // if this array contains our hex, dump all string siblings
    const strs=node.filter(x=>typeof x==="string");
    if(strs.some(s=>s.includes(hex)||s===tidA||s===tidF||s.startsWith("Ktbx"))){
      acc.push({path, siblingStrs: strs.map(s=>s.slice(0,100)), note:"array-with-anchor"});
    }
    for(let i=0;i<node.length;i++) walkFind(node[i], path+"["+i+"]", acc, depth+1);
    return;
  }
  if(node&&typeof node==="object"){
    for(const k of Object.keys(node)) walkFind(node[k], path+"."+k, acc, depth+1);
  }
}

const parsedHits=[];
for(let i=0;i<__g.bv.length;i++){
  try{
    let t=__g.bv[i].replace(/^\)\]\}'\n/,"");
    const j=JSON.parse(t);
    const acc=[]; walkFind(j,"$",acc,0);
    parsedHits.push({i, n:acc.length, acc:acc.slice(0,40)});
  }catch(e){
    // try eval Gmail loose
    parsedHits.push({i, err:String(e).slice(0,100), head:String(__g.bv[i]).slice(0,80)});
  }
}

// raw includes Ktbx?
const ktbx=[];
for(let i=0;i<__g.bv.length;i++){
  const ms=String(__g.bv[i]).match(/Ktbx[A-Za-z0-9]{10,}/g);
  if(ms) ktbx.push({i, ms:[...new Set(ms)].slice(0,10)});
}

return {nBv:__g.bv.length, bvLens:__g.bv.map(t=>t.length), ktbx, parsedHits};
