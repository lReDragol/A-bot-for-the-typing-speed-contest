// ==UserScript==
// @name         AutoParser Integration with Python
// @namespace    http://tampermonkey.net/
// @version      5.0
// @description  Автоматически извлекает слова, проверяет разрешение и отправляет их каждые 2 секунды (учитывая force_parse и memory_enabled)
// @author       https://github.com/lReDragol
// @match        https://blindtyping.com/ru/test
// @match        https://play.typeracer.com/*
// @match        https://gonki.nabiraem.ru/*
// @match        https://www.speedcoder.net/*
// @match        https://fastfingers.net/*
// @match        https://www.speedtypingonline.com/*
// @grant        none
// ==/UserScript==

(function () {
    'use strict';

    const CHECK_INTERVAL = 2000; // 2 секунды
    const PARSING_STATUS_URL = 'http://127.0.0.1:5000/parsing_status';
    const SEND_WORDS_URL = 'http://127.0.0.1:5000/words';

    // Флаг «принудительного парсинга» и «запоминания» — будем получать от сервера
    let memoryEnabled = true;

    function getStorageKey() {
        return 'lastSentIndex_' + window.location.hostname;
    }

    async function checkParsingStatus() {
        try {
            const r = await fetch(PARSING_STATUS_URL);
            const data = await r.json();
            const canParse = data.enabled === true;
            memoryEnabled = data.memory_enabled !== false;
            // Если force=true, сбросим lastSentIndex, чтобы точно отправить всё заново
            if (data.force === true) {
                console.log("[TM] FORCE parse triggered, сбрасываем индекс");
                localStorage.removeItem(getStorageKey());
            }
            return canParse;
        } catch {
            return false;
        }
    }

    async function sendWordsToServer(words) {
        if (!words.length) return;
        try {
            await fetch(SEND_WORDS_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ words })
            });
        } catch (e) {
            console.error('[TM] Ошибка отправки слов на сервер:', e);
        }
    }

    // ---- Парсеры ----
    function extractFromBlindTyping() {
        const c = document.getElementById('words');
        if (!c) return [];
        const divs = c.querySelectorAll('div.TestWrapper_word__TI39_');
        let out = [];
        divs.forEach(d => {
            let w = '';
            d.querySelectorAll('span').forEach(s => w += s.textContent);
            out.push(w);
        });
        return out;
    }
    function extractFromTyperacer() {
        const sels = [
            'div.kdLjEPtI.gahazPRK',
            'div[style*="font-size: 20px"][style*="font-family: monospace"]',
            'div[class*="gahazPRK"]'
        ];
        let tc = null, txt='';
        for(let sel of sels){
            tc = document.querySelector(sel);
            if(tc) break;
        }
        if(!tc) return [];
        tc.querySelectorAll('span').forEach(sp => txt+=sp.textContent);
        if(!txt.trim()) return [];
        return txt.trim().split(/\s+/);
    }
    function extractFromGonki() {
        const c = document.querySelector('div.editor-text');
        if(!c) return [];
        let out = [];
        c.querySelectorAll('span.word').forEach(s=>{
            out.push(s.textContent.replace(/˽/g,''));
        });
        return out;
    }
    function extractFromSpeedcoder() {
        const pre = document.querySelector('pre#main');
        if(!pre) return [];
        let out = [];
        let curr = '';
        pre.querySelectorAll('spanchar, tabchar').forEach(sp => {
            if(sp.tagName==='SPANCHAR'){
                if(sp.classList.contains('ret')){
                    if(curr.trim()) out.push(curr);
                    curr='';
                    out.push('');
                } else if(sp.textContent===' '){
                    if(curr.trim()) out.push(curr);
                    curr='';
                } else {
                    curr+=sp.textContent;
                }
            } else if(sp.tagName==='TABCHAR'){
                if(curr.trim()) out.push(curr);
                curr='';
                out.push('\t');
            }
        });
        if(curr.trim()) out.push(curr);
        return out;
    }
    function extractFromFastfingers() {
        const w = document.getElementById('wordWrapper');
        if(!w) return [];
        let out=[];
        w.querySelectorAll('div.word').forEach(d=>{
            let txt='';
            d.querySelectorAll('letter').forEach(l=> txt+=l.textContent);
            out.push(txt);
        });
        return out;
    }
    function extractFromSpeedTypingOnline() {
        const c = document.getElementById('lineDivContainer');
        if(!c) return [];
        const lines = c.querySelectorAll('.blockLines');
        let out=[];
        lines.forEach(line=>{
            let tmp='';
            line.querySelectorAll('.nxtLetter, .plainText').forEach(sp=>{
                const t = sp.textContent;
                if(sp.classList.contains('nxtLetter')){
                    if(t) tmp+=t;
                } else {
                    if(t.trim()===''){
                        if(tmp.length>0){
                            out.push(tmp);
                            tmp='';
                        }
                    } else {
                        tmp+=t;
                    }
                }
            });
            if(tmp.length>0) out.push(tmp);
        });
        return out;
    }

    function extractText() {
        const url = location.href;
        if(url.includes('blindtyping.com')) return extractFromBlindTyping();
        if(url.includes('play.typeracer.com')) return extractFromTyperacer();
        if(url.includes('gonki.nabiraem.ru')) return extractFromGonki();
        if(url.includes('speedcoder.net')) return extractFromSpeedcoder();
        if(url.includes('fastfingers.net')) return extractFromFastfingers();
        if(url.includes('speedtypingonline.com')) return extractFromSpeedTypingOnline();
        return [];
    }

    function startAutoSend() {
        let lastSentIndex = parseInt(localStorage.getItem(getStorageKey()))||0;

        setInterval(async ()=>{
            const canParse = await checkParsingStatus();
            if(!canParse) return;

            const words = extractText();
            if(!words.length) return;

            // Если текста стало меньше, сброс
            if(words.length<lastSentIndex){
                lastSentIndex=0;
            }
            // Если память выключена, будем всегда отправлять всё
            let newWords = memoryEnabled ? words.slice(lastSentIndex) : words;

            if(newWords.length){
                await sendWordsToServer(newWords);
                if(memoryEnabled){
                    lastSentIndex += newWords.length;
                    localStorage.setItem(getStorageKey(), String(lastSentIndex));
                }
            }
        }, CHECK_INTERVAL);
    }

    startAutoSend();
})();
