const fs = require('fs');
const path = require('path');

const cssFiles = ['styles.css', 'admin.css', 'admin-extra.css', 'tools.css'];
const basePath = 'C:\\Users\\Administrator\\.gemini\\antigravity\\scratch\\thaitu\\miniapp\\assets';

for (const file of cssFiles) {
    let content = fs.readFileSync(path.join(basePath, file), 'utf8');

    // Root variables (in styles.css)
    if (file === 'styles.css') {
        content = content.replace(/:root\{.*?\}/, ':root{--bg:#f8fafc;--card:#ffffff;--ink:#1e293b;--muted:#94a3b8;--line:#e2e8f0;--green:#10b981;--dark:#1e293b;--neon:#a78bfa;--primary:#6366f1;--primary-gradient:linear-gradient(135deg, #6366f1, #8b5cf6);--shadow:0 4px 6px -1px rgba(0,0,0,0.07);--radius:16px;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg);font-synthesis:none}');
        
        // Body
        content = content.replace(/background:linear-gradient\(180deg,#f8fbfa 0,#f1f6f3 100%\)/, 'background:var(--bg)');
        
        // Brand mark
        content = content.replace(/background:linear-gradient\(145deg,#09291f,#03140f\)/g, 'background:var(--primary-gradient)');
        content = content.replace(/color:var\(--neon\)/g, 'color:#fff'); // adjust neon color usage to white where appropriate

        // Balance card
        content = content.replace(/background:radial-gradient\(circle at 90% 0,#174d39 0,transparent 38%\),linear-gradient\(135deg,#09271e,#061a14\)/, 'background:linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #a78bfa 100%)');
        content = content.replace(/box-shadow:0 22px 42px rgba\(7,38,28,\.2\)/, 'box-shadow:0 10px 15px -3px rgba(99, 102, 241, 0.4)');
        
        // Buttons
        content = content.replace(/\.button\{border:0;border-radius:15px;background:var\(--neon\);/, '.button{border:0;border-radius:12px;background:var(--primary-gradient);color:#fff;box-shadow:0 4px 6px -1px rgba(99,102,241,0.2);transition:transform 0.2s, box-shadow 0.2s;}.button:hover{transform:scale(1.02);box-shadow:0 10px 15px -3px rgba(99,102,241,0.3);} .button{');
        
        // Quick grid
        content = content.replace(/background:#f0e9ff;color:#785ad2/, 'background:#eef2ff;color:#6366f1');
        content = content.replace(/background:#e2f3ff;color:#228bb6/, 'background:#ecfdf5;color:#10b981');
        content = content.replace(/background:#e0f7ee;color:#079a70/, 'background:#fffbeb;color:#f59e0b');
        // Add 4th pastel color if we can match it:
        // .quick-grid>button>i matches background:#f0e9ff etc. We'll leave it as is if replaced above.

        // Social card
        content = content.replace(/background:linear-gradient\(105deg,#083427,#0d4c38\)/, 'background:var(--primary-gradient)');

        // Product image
        content = content.replace(/background:linear-gradient\(135deg,#08281e,#092018\)/, 'background:linear-gradient(135deg, #f8fafc, #e2e8f0)');

        // Product placeholder
        content = content.replace(/color:var\(--neon\)/, 'color:var(--primary)');

        // Category row active
        content = content.replace(/background:#071f18;color:var\(--neon\)/, 'background:var(--primary);color:#fff');

        // Privacy banner
        content = content.replace(/background:#def4eb;border:1px solid #b6dfd0;color:#06956d/, 'background:#eef2ff;border:1px solid #c7d2fe;color:#4f46e5');

        // Loading screen
        content = content.replace(/background:#dce3e0/, 'background:#e2e8f0');
        content = content.replace(/background:linear-gradient\(90deg,#0b8c66,var\(--neon\)\)/, 'background:var(--primary-gradient)');

        // Modal backdrop
        content = content.replace(/rgba\(4,20,15,\.47\)/, 'rgba(15, 23, 42, 0.5)');

        // Toast
        content = content.replace(/background:#073326;color:white;box-shadow:0 12px 30px #0002/, 'background:#fff;color:var(--ink);border-left:4px solid var(--primary);box-shadow:0 10px 15px -3px rgba(0,0,0,0.1)');
        content = content.replace(/background:#a62f36/, 'border-left-color:var(--danger)');

        // Bottom nav
        content = content.replace(/background:rgba\(255,252,254,\.96\)/, 'background:rgba(255, 255, 255, 0.96)');
        content = content.replace(/color:#007c58/, 'color:var(--primary)');
        content = content.replace(/background:#16b17e/, 'background:var(--primary)');
        
        // Add subtle hover to buttons
        content += '\nbutton:active{transform:scale(0.97)}';
    } else if (file === 'admin.css') {
        // Admin entry button
        content = content.replace(/background:linear-gradient\(120deg,#071f18,#134c39\)/, 'background:linear-gradient(135deg, #6366f1, #8b5cf6)');
        
        // Admin page bg
        content = content.replace(/background:linear-gradient\(180deg,#f5faf7,#eef5f1\)/, 'background:#f8fafc');
        
        // Admin stats top accent border
        content = content.replace(/border:1px solid var\(--line\)/, 'border:1px solid var(--line);border-top:3px solid var(--primary)');
        
        // Status badges
        content = content.replace(/background:#fde7e8;color:#a83137/, 'background:#fee2e2;color:#ef4444'); // reject
        content = content.replace(/background:#daf6e9;color:#087d59/, 'background:#d1fae5;color:#10b981'); // approve
        
        content = content.replace(/background:#dff4eb;color:#087b5a/, 'background:#eef2ff;color:#6366f1'); // ticket/search buttons
    } else if (file === 'admin-extra.css') {
        // Upload zone
        content = content.replace(/border:2px dashed #bdd8cc;background:#f4faf7/, 'border:2px dashed var(--primary);background:#f8fafc;transition:background 0.2s;');
        content += '\n.upload-zone:hover{background:#eef2ff;}';
        
        // Inventory cards
        content = content.replace(/background: #f5f9f7;/, 'background: #ffffff; border-top: 3px solid var(--primary);');
        content = content.replace(/background: #143f31;/, 'background: var(--primary);');
        
        // Audit rows
        content = content.replace(/border-left: 3px solid #15a878;/, 'border-left: 3px solid var(--primary);');
        content = content.replace(/background: #f5f9f7;/, 'background: #ffffff;');
        
        // Feature switches
        content = content.replace(/background: #f2f7f4;/, 'background: #e2e8f0; border-radius: 16px; transition: background 0.2s;');
        
        // Upload progress
        content = content.replace(/border-top-color:#0a9a6f/, 'border-top-color:var(--primary)');
    } else if (file === 'tools.css') {
        // Tool stock indicators
        content = content.replace(/background: #fff;/, 'background: #ffffff; border-left: 3px solid var(--primary);');
        
        // Wallet hero
        content = content.replace(/background:linear-gradient\(135deg,#062f25,#0b654c\)/, 'background:linear-gradient(135deg, #6366f1, #8b5cf6)');
        
        // Deposit presets
        content = content.replace(/background:#f6faf8;color:#38564c/, 'background:#f8fafc;color:var(--primary)');
        content = content.replace(/background:#dff7c4;border-color:#94d457;color:#355611/, 'background:#eef2ff;border-color:var(--primary);color:var(--primary)');
        
        // Form fields focus
        content = content.replace(/border-color: var\(--green\);\n\s*box-shadow: 0 0 0 3px rgba\(8,123,90,\.1\);/, 'border-color: var(--primary);\n  box-shadow: 0 0 0 3px rgba(99,102,241,0.1);');
        
        // Result textarea
        content = content.replace(/background: #f6faf8;/, 'background: #ffffff;');
        
        // Status badges
        content = content.replace(/border-left-color:#efa929;background:#fffaf0/, 'border-left-color:#f59e0b;background:#fffbeb');
        content = content.replace(/border-left-color:#08a777;background:#f2fbf7/, 'border-left-color:#10b981;background:#ecfdf5');
        content = content.replace(/border-left-color:#d64d55;background:#fff5f5/, 'border-left-color:#ef4444;background:#fef2f2');
        
        // Payment button
        content = content.replace(/background:linear-gradient\(120deg,#92e300,#b4f33e\)!important;color:#173f18!important/, 'background:linear-gradient(135deg, #6366f1, #8b5cf6)!important;color:#fff!important');
    }

    fs.writeFileSync(path.join(basePath, file), content, 'utf8');
}
console.log('CSS files updated!');
