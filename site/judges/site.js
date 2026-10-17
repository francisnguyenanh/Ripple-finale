(function(){
  function status(msg){var s=document.getElementById('copystatus');if(s){s.textContent=msg;}}
  function selectText(el){var r=document.createRange();r.selectNodeContents(el);var sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);}
  document.addEventListener('click',function(e){
    var b=e.target.closest('[data-copy-target]');
    if(b){var el=document.getElementById(b.getAttribute('data-copy-target'));var text=el.innerText.trim();selectText(el);
      var done=function(){status('Copied. Paste it into the chat box.');};
      var fail=function(){status('Copy was blocked by the browser. The text is selected: press Ctrl+C (or Cmd+C) to copy it.');};
      if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done,function(){try{document.execCommand('copy')?done():fail();}catch(x){fail();}});}
      else{try{document.execCommand('copy')?done():fail();}catch(x){fail();}}
      return;}
    var x=e.target.closest('[data-expand]');
    if(x){var open=x.getAttribute('data-expand')==='1';document.querySelectorAll('.stepcard details').forEach(function(d){d.open=open;});}
  });
})();
