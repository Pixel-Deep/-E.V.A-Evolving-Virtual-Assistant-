"""Deterministic professional website builder for EVA.

Produces high-quality, responsive websites by assembling pre-built
professional templates and injecting LLM-generated content. The LLM
only provides text content (headlines, descriptions, section names),
never HTML/CSS/JS code. This keeps output quality consistent and
reduces model pressure significantly.
"""
from __future__ import annotations

import json
import re
from typing import Any


# ---------------------------------------------------------------------------
# Theme system
# ---------------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "default": {
        "bg": "#fbfaf6",
        "primary": "#173f35",
        "primary_light": "#234f3d",
        "ink": "#20332b",
        "muted": "#65736b",
        "accent": "#a45b38",
        "accent_light": "#b96545",
        "amber": "#f2c48d",
        "surface": "#f0f3e9",
        "surface_alt": "#e1ebe0",
        "art_bg": "#d8e5d1",
        "card_bg": "#ffffff",
        "card_border": "#e5e8df",
        "cta_bg": "#173f35",
        "cta_text": "#ffffff",
        "cta_accent": "#efbf8a",
        "cta_muted": "#dce7df",
        "btn_bg": "#173f35",
        "btn_text": "#ffffff",
        "btn_light_bg": "#f1bf82",
        "btn_light_text": "#173f35",
        "art_sun": "#e9ad69",
        "art_one": "#bc6849",
        "art_two": "#456d5b",
        "art_three": "#e5b65f",
        "art_plant": "#456d5b",
        "art_leaf": "#719174",
        "font_display": "Georgia, serif",
        "font_body": "system-ui, -apple-system, Segoe UI, sans-serif",
    },
    "ocean": {
        "bg": "#f7f9fc",
        "primary": "#1a3a5c",
        "primary_light": "#2a5a8c",
        "ink": "#1a2a3c",
        "muted": "#5a6a7a",
        "accent": "#c4652a",
        "accent_light": "#d4753a",
        "amber": "#f0c87a",
        "surface": "#e8f0f8",
        "surface_alt": "#d0e0f0",
        "art_bg": "#c8d8e8",
        "card_bg": "#ffffff",
        "card_border": "#d8e0e8",
        "cta_bg": "#1a3a5c",
        "cta_text": "#ffffff",
        "cta_accent": "#f0c87a",
        "cta_muted": "#c0d0e0",
        "btn_bg": "#1a3a5c",
        "btn_text": "#ffffff",
        "btn_light_bg": "#f0c87a",
        "btn_light_text": "#1a3a5c",
        "art_sun": "#f0c87a",
        "art_one": "#2a5a8c",
        "art_two": "#3a7aac",
        "art_three": "#c4652a",
        "art_plant": "#2a6a5c",
        "art_leaf": "#4a8a7c",
        "font_display": "Georgia, serif",
        "font_body": "system-ui, -apple-system, Segoe UI, sans-serif",
    },
    "warm": {
        "bg": "#faf8f5",
        "primary": "#5c2a1a",
        "primary_light": "#7a3a2a",
        "ink": "#3c2a1a",
        "muted": "#7a6a5a",
        "accent": "#b86a30",
        "accent_light": "#c87a40",
        "amber": "#f0c87a",
        "surface": "#f5ede5",
        "surface_alt": "#e8dcd0",
        "art_bg": "#e0d0c0",
        "card_bg": "#ffffff",
        "card_border": "#e8dcd0",
        "cta_bg": "#5c2a1a",
        "cta_text": "#ffffff",
        "cta_accent": "#f0c87a",
        "cta_muted": "#e0d0c0",
        "btn_bg": "#5c2a1a",
        "btn_text": "#ffffff",
        "btn_light_bg": "#f0c87a",
        "btn_light_text": "#5c2a1a",
        "art_sun": "#f0c87a",
        "art_one": "#c87a40",
        "art_two": "#7a3a2a",
        "art_three": "#e8b060",
        "art_plant": "#5a7a4a",
        "art_leaf": "#7a9a6a",
        "font_display": "Georgia, serif",
        "font_body": "system-ui, -apple-system, Segoe UI, sans-serif",
    },
    "cool": {
        "bg": "#f8f8fc",
        "primary": "#2a1a5c",
        "primary_light": "#3a2a7a",
        "ink": "#1a1a3c",
        "muted": "#6a5a7a",
        "accent": "#a04a8c",
        "accent_light": "#b05a9c",
        "amber": "#e8b0d0",
        "surface": "#f0e8f5",
        "surface_alt": "#e0d0f0",
        "art_bg": "#d8c8e8",
        "card_bg": "#ffffff",
        "card_border": "#e0d8e8",
        "cta_bg": "#2a1a5c",
        "cta_text": "#ffffff",
        "cta_accent": "#e8b0d0",
        "cta_muted": "#d0c0e0",
        "btn_bg": "#2a1a5c",
        "btn_text": "#ffffff",
        "btn_light_bg": "#e8b0d0",
        "btn_light_text": "#2a1a5c",
        "art_sun": "#e8b0d0",
        "art_one": "#7a4a9c",
        "art_two": "#3a2a7a",
        "art_three": "#c88ab0",
        "art_plant": "#4a7a5a",
        "art_leaf": "#6a9a7a",
        "font_display": "Georgia, serif",
        "font_body": "system-ui, -apple-system, Segoe UI, sans-serif",
    },
    "dark": {
        "bg": "#1a1a2e",
        "primary": "#e0d0a0",
        "primary_light": "#c8b888",
        "ink": "#e8e0d0",
        "muted": "#a0a0b0",
        "accent": "#c87a40",
        "accent_light": "#d88a50",
        "amber": "#f0c87a",
        "surface": "#252540",
        "surface_alt": "#303050",
        "art_bg": "#353560",
        "card_bg": "#252540",
        "card_border": "#404060",
        "cta_bg": "#c87a40",
        "cta_text": "#1a1a2e",
        "cta_accent": "#f0c87a",
        "cta_muted": "#e8e0d0",
        "btn_bg": "#c87a40",
        "btn_text": "#1a1a2e",
        "btn_light_bg": "#f0c87a",
        "btn_light_text": "#1a1a2e",
        "art_sun": "#f0c87a",
        "art_one": "#c87a40",
        "art_two": "#5a7a9a",
        "art_three": "#a06050",
        "art_plant": "#4a8a6a",
        "art_leaf": "#6aaa8a",
        "font_display": "Georgia, serif",
        "font_body": "system-ui, -apple-system, Segoe UI, sans-serif",
    },
}


# ---------------------------------------------------------------------------
# CSS illustration variants for the hero art
# ---------------------------------------------------------------------------

ILLUSTRATION_VARIANTS: dict[str, dict[str, str]] = {
    "books": {
        "shapes": (
            '<div class="sun"></div>'
            '<div class="book book-one"></div>'
            '<div class="book book-two"></div>'
            '<div class="book book-three"></div>'
            '<div class="plant"></div>'
        ),
        "css": (
            ".sun{position:absolute;width:100px;height:100px;border-radius:50%;right:18%;top:13%;background:var(--art-sun)}"
            ".book{position:absolute;bottom:19%;height:150px;width:25%;border-radius:12px 12px 4px 4px;transform:rotate(-9deg);box-shadow:inset 8px 0 #ffffff48}"
            ".book-one{left:18%;background:var(--art-one)}"
            ".book-two{left:39%;height:190px;background:var(--art-two);transform:rotate(5deg)}"
            ".book-three{left:59%;height:125px;background:var(--art-three);transform:rotate(12deg)}"
            ".plant{position:absolute;width:8px;height:112px;right:12%;bottom:14%;background:var(--art-plant);transform:rotate(8deg)}"
            ".plant:before,.plant:after{content:\"\";position:absolute;width:42px;height:20px;border-radius:100% 0;background:var(--art-leaf);left:-36px;top:18px;transform:rotate(-25deg)}"
            ".plant:after{top:54px;left:0;transform:rotate(25deg)}"
        ),
    },
    "geometric": {
        "shapes": (
            '<div class="geo-circle"></div>'
            '<div class="geo-rect geo-rect-one"></div>'
            '<div class="geo-rect geo-rect-two"></div>'
            '<div class="geo-tri"></div>'
            '<div class="geo-dot geo-dot-one"></div>'
            '<div class="geo-dot geo-dot-two"></div>'
        ),
        "css": (
            ".geo-circle{position:absolute;width:140px;height:140px;border-radius:50%;right:15%;top:15%;background:var(--art-sun);opacity:.85}"
            ".geo-rect{position:absolute;border-radius:12px}"
            ".geo-rect-one{width:100px;height:160px;left:15%;bottom:18%;background:var(--art-one);transform:rotate(-12deg)}"
            ".geo-rect-two{width:80px;height:120px;left:42%;bottom:22%;background:var(--art-two);transform:rotate(8deg)}"
            ".geo-tri{position:absolute;right:22%;bottom:16%;width:0;height:0;border-left:55px solid transparent;border-right:55px solid transparent;border-bottom:95px solid var(--art-three)}"
            ".geo-dot{position:absolute;border-radius:50%}"
            ".geo-dot-one{width:24px;height:24px;left:35%;top:20%;background:var(--art-plant)}"
            ".geo-dot-two{width:16px;height:16px;right:38%;top:35%;background:var(--art-leaf)}"
        ),
    },
    "waves": {
        "shapes": (
            '<div class="wave-sun"></div>'
            '<div class="wave-band wave-one"></div>'
            '<div class="wave-band wave-two"></div>'
            '<div class="wave-band wave-three"></div>'
        ),
        "css": (
            ".wave-sun{position:absolute;width:110px;height:110px;border-radius:50%;right:20%;top:12%;background:var(--art-sun)}"
            ".wave-band{position:absolute;left:0;right:0;height:60px;border-radius:50%}"
            ".wave-one{bottom:10%;background:var(--art-one);opacity:.6}"
            ".wave-two{bottom:20%;background:var(--art-two);opacity:.5}"
            ".wave-three{bottom:30%;background:var(--art-three);opacity:.4}"
        ),
    },
    "abstract": {
        "shapes": (
            '<div class="abs-blob abs-one"></div>'
            '<div class="abs-blob abs-two"></div>'
            '<div class="abs-blob abs-three"></div>'
            '<div class="abs-ring"></div>'
        ),
        "css": (
            ".abs-blob{position:absolute;border-radius:48% 52% 41% 59%/53% 45% 55% 47%}"
            ".abs-one{width:160px;height:140px;left:10%;top:15%;background:var(--art-one);opacity:.7}"
            ".abs-two{width:120px;height:130px;right:12%;top:25%;background:var(--art-two);opacity:.6}"
            ".abs-three{width:100px;height:90px;left:35%;bottom:15%;background:var(--art-three);opacity:.65}"
            ".abs-ring{position:absolute;width:80px;height:80px;border-radius:50%;border:6px solid var(--art-sun);right:30%;top:10%;opacity:.7}"
        ),
    },
}


# ---------------------------------------------------------------------------
# Layout builders
# ---------------------------------------------------------------------------

def _select_theme(name: str | None) -> dict[str, str]:
    if not name or name not in THEMES:
        return THEMES["default"]
    return THEMES[name]


def _select_illustration(name: str | None) -> dict[str, str]:
    if not name or name not in ILLUSTRATION_VARIANTS:
        return ILLUSTRATION_VARIANTS["books"]
    return ILLUSTRATION_VARIANTS[name]


def _detect_layout(request: str) -> str:
    req = request.lower()
    if re.search(r"\b(portfolio|showcase|gallery|my work|personal site)\b", req):
        return "portfolio"
    if re.search(r"\b(dashboard|admin|panel|analytics|metrics)\b", req):
        return "dashboard"
    if re.search(r"\b(blog|article|news|magazine|journal)\b", req):
        return "blog"
    if re.search(r"\b(restaurant|cafe|bakery|food|menu|dining)\b", req):
        return "restaurant"
    return "landing"


def _detect_theme(request: str) -> str:
    req = request.lower()
    if re.search(r"\b(dark|night|midnight)\b", req):
        return "dark"
    if re.search(r"\b(ocean|sea|marine|water|blue)\b", req):
        return "ocean"
    if re.search(r"\b(warm|cozy|rustic|earth)\b", req):
        return "warm"
    if re.search(r"\b(cool|purple|violet|lavender)\b", req):
        return "cool"
    return "default"


def _detect_illustration(request: str) -> str:
    req = request.lower()
    if re.search(r"\b(school|education|learn|study|book|academy|college|university)\b", req):
        return "books"
    if re.search(r"\b(tech|startup|saas|software|digital|app)\b", req):
        return "geometric"
    if re.search(r"\b(nature|ocean|sea|travel|outdoor|relax)\b", req):
        return "waves"
    return "abstract"


# ---------------------------------------------------------------------------
# CSS generation
# ---------------------------------------------------------------------------

def _build_css(theme: dict[str, str], illustration_css: str) -> str:
    return (
        ":root{"
        f"--bg:{theme['bg']};--primary:{theme['primary']};--primary-light:{theme['primary_light']};"
        f"--ink:{theme['ink']};--muted:{theme['muted']};--accent:{theme['accent']};"
        f"--accent-light:{theme['accent_light']};--amber:{theme['amber']};--surface:{theme['surface']};"
        f"--surface-alt:{theme['surface_alt']};--art-bg:{theme['art_bg']};--card-bg:{theme['card_bg']};"
        f"--card-border:{theme['card_border']};--cta-bg:{theme['cta_bg']};--cta-text:{theme['cta_text']};"
        f"--cta-accent:{theme['cta_accent']};--cta-muted:{theme['cta_muted']};--btn-bg:{theme['btn_bg']};"
        f"--btn-text:{theme['btn_text']};--btn-light-bg:{theme['btn_light_bg']};--btn-light-text:{theme['btn_light_text']};"
        f"--art-sun:{theme['art_sun']};--art-one:{theme['art_one']};--art-two:{theme['art_two']};"
        f"--art-three:{theme['art_three']};--art-plant:{theme['art_plant']};--art-leaf:{theme['art_leaf']};"
        f"--font-display:{theme['font_display']};--font-body:{theme['font_body']};"
        "--content-width:1120px;--gutter:max(5vw,1.25rem);--gutter-wide:max(8vw,1.5rem)}"
        "*{box-sizing:border-box}"
        "html{scroll-behavior:smooth}"
        "body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 var(--font-body)}"
        "@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*{animation:none!important;transition:none!important}}"
        ".topbar{height:76px;padding:0 var(--gutter);display:flex;align-items:center;justify-content:space-between;background:var(--bg);max-width:var(--content-width);margin:0 auto}"
        ".brand{font-weight:800;color:var(--primary);text-decoration:none;font-size:1.1rem}"
        ".nav-links{display:flex;gap:1.5rem;align-items:center}"
        ".nav-links a{color:var(--primary);text-decoration:none;font-weight:600;font-size:.95rem;transition:opacity .2s}"
        ".nav-links a:hover{opacity:.7}"
        ".nav-cta{color:var(--btn-text);background:var(--btn-bg);text-decoration:none;font-weight:700;padding:.55rem 1.1rem;border-radius:999px;font-size:.9rem;transition:opacity .2s}"
        ".nav-cta:hover{opacity:.88}"
        ".mobile-toggle{display:none;background:none;border:none;cursor:pointer;padding:.5rem}"
        ".mobile-toggle span{display:block;width:22px;height:2px;background:var(--primary);margin:5px 0;transition:transform .3s}"
        ".hero{display:grid;grid-template-columns:1.05fr .95fr;align-items:center;gap:3rem;min-height:560px;padding:4rem var(--gutter-wide);background:var(--surface)}"
        ".hero-copy{max-width:600px}"
        ".eyebrow{color:var(--accent);font-size:.75rem;font-weight:800;letter-spacing:.16em;text-transform:uppercase;margin:0}"
        ".hero h1{font-family:var(--font-display);font-size:clamp(2.7rem,5vw,4.8rem);line-height:1.06;letter-spacing:-.04em;margin:.75rem 0 1.25rem}"
        ".hero h1 span{color:var(--accent)}"
        ".lead{max-width:32rem;font-size:1.13rem;color:var(--muted);line-height:1.7}"
        ".button{display:inline-flex;gap:1rem;align-items:center;margin-top:1rem;padding:.85rem 1.2rem;border-radius:999px;background:var(--btn-bg);color:var(--btn-text);text-decoration:none;font-weight:750;transition:opacity .2s;border:none;cursor:pointer;font-size:1rem}"
        ".button:hover{opacity:.88}"
        ".button:focus-visible{outline:3px solid var(--accent);outline-offset:2px}"
        ".date-note{margin-top:1.2rem;color:var(--muted);font-weight:700}"
        ".hero-art{position:relative;min-height:330px;border-radius:48% 52% 41% 59%/53% 45% 55% 47%;background:var(--art-bg);overflow:hidden}"
        f"{illustration_css}"
        ".intro{max-width:760px;margin:auto;padding:5rem 1.5rem 2rem;text-align:center}"
        ".intro h2,.admissions h2,.section-heading{font:700 clamp(2rem,4vw,3.1rem)/1.12 var(--font-display);margin:.6rem 0 1rem}"
        ".intro>p:last-child{max-width:620px;margin:auto;color:var(--muted);line-height:1.7}"
        ".features{display:grid;grid-template-columns:repeat(3,1fr);gap:1.25rem;max-width:var(--content-width);margin:1rem auto;padding:2rem 1.5rem 5rem}"
        ".features article{padding:1.5rem;border:1px solid var(--card-border);border-radius:1rem;background:var(--card-bg);transition:box-shadow .2s}"
        ".features article:hover{box-shadow:0 8px 24px rgba(0,0,0,.06)}"
        ".icon{color:var(--accent);font-weight:800;letter-spacing:.12em;font-size:1.1rem}"
        ".features h3{font:700 1.35rem var(--font-display);margin:.9rem 0 .4rem}"
        ".features p{margin:0;color:var(--muted);line-height:1.65}"
        ".admissions{display:flex;align-items:center;justify-content:space-between;gap:2rem;padding:3rem var(--gutter-wide);background:var(--cta-bg);color:var(--cta-text)}"
        ".admissions .eyebrow{color:var(--cta-accent)}"
        ".admissions>div{max-width:680px}"
        ".admissions p:not(.eyebrow){color:var(--cta-muted)}"
        ".button-light{background:var(--btn-light-bg);color:var(--btn-light-text);white-space:nowrap}"
        "footer{padding:2.5rem var(--gutter-wide);color:var(--muted)}"
        "footer p{margin:.25rem 0}"
        "footer strong{color:var(--primary)}"
        "footer .footer-grid{display:grid;grid-template-columns:1.2fr 1fr 1fr;gap:2rem;max-width:var(--content-width);margin:0 auto}"
        "footer .footer-grid h4{font:700 1rem var(--font-display);color:var(--primary);margin:0 0 .5rem}"
        "footer .footer-grid a{color:var(--muted);text-decoration:none;transition:color .2s}"
        "footer .footer-grid a:hover{color:var(--primary)}"
        "footer .footer-grid ul{list-style:none;padding:0;margin:0}"
        "footer .footer-grid li{margin:.3rem 0}"
        "@media(max-width:760px){"
        ".hero{grid-template-columns:1fr;padding-top:3rem;gap:2rem}"
        ".hero-art{min-height:260px}"
        ".features{grid-template-columns:1fr}"
        ".admissions{align-items:flex-start;flex-direction:column}"
        ".topbar{height:64px}"
        ".nav-links{display:none;position:absolute;top:64px;left:0;right:0;background:var(--bg);flex-direction:column;padding:1rem var(--gutter);box-shadow:0 8px 24px rgba(0,0,0,.08);z-index:100}"
        ".nav-links.open{display:flex}"
        ".mobile-toggle{display:block}"
        "footer .footer-grid{grid-template-columns:1fr}"
        "}"
    )


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_landing_html(content: dict[str, Any], illustration_shapes: str) -> str:
    brand = _esc(content.get("brand_name", "My Website"))
    nav_links_html = ""
    nav_items = content.get("nav_links", [])
    if isinstance(nav_items, list):
        for item in nav_items[:4]:
            if isinstance(item, dict):
                nav_links_html += f'<a href="{_esc(item.get("href", "#"))}">{_esc(item.get("text", ""))}</a>'

    hero_eyebrow = _esc(content.get("hero_eyebrow", "Welcome"))
    hero_headline = _esc(content.get("hero_headline", "Something Amazing"))
    hero_accent = _esc(content.get("hero_headline_accent", "starts here"))
    hero_lead = _esc(content.get("hero_lead", "Discover what we have to offer."))
    hero_cta = _esc(content.get("hero_cta_text", "Get Started"))
    hero_cta_link = _esc(content.get("hero_cta_link", "#cta"))
    hero_note = content.get("hero_date_note", "")
    hero_note_html = f'<p class="date-note">{_esc(hero_note)}</p>' if hero_note else ""
    art_label = _esc(content.get("hero_art_label", "Decorative illustration"))

    intro_eyebrow = _esc(content.get("intro_eyebrow", "ABOUT US"))
    intro_heading = _esc(content.get("intro_heading", "What we do"))
    intro_text = _esc(content.get("intro_text", "We provide exceptional service and quality."))

    features = content.get("features", [])
    if not isinstance(features, list) or len(features) < 1:
        features = [
            {"title": "Quality First", "description": "We deliver excellence in everything we do."},
            {"title": "Expert Team", "description": "Our professionals bring years of experience."},
            {"title": "Results Driven", "description": "We focus on outcomes that matter to you."},
        ]
    features_html = ""
    for i, feat in enumerate(features[:6]):
        if isinstance(feat, dict):
            num = f"{i + 1:02d}"
            title = _esc(str(feat.get("title", f"Feature {i+1}")))
            desc = _esc(str(feat.get("description", "")))
            features_html += f'<article><span class="icon">{num}</span><h3>{title}</h3><p>{desc}</p></article>'

    cta_eyebrow = _esc(content.get("cta_eyebrow", "TAKE ACTION"))
    cta_heading = _esc(content.get("cta_heading", "Ready to get started?"))
    cta_text = _esc(content.get("cta_text", "Get in touch with us today."))
    cta_button = _esc(content.get("cta_button_text", "Contact Us"))
    cta_link = _esc(content.get("cta_link", "#contact"))

    footer_brand = brand
    footer_desc = _esc(content.get("footer_text", "Get in touch to learn more."))
    footer_links = content.get("footer_links", [])
    footer_links_html = ""
    if isinstance(footer_links, list) and footer_links:
        for group in footer_links[:2]:
            if isinstance(group, dict):
                title = _esc(str(group.get("title", "")))
                items = group.get("items", [])
                items_html = ""
                for li in items[:5]:
                    if isinstance(li, dict):
                        items_html += f'<li><a href="{_esc(li.get("href", "#"))}">{_esc(li.get("text", ""))}</a></li>'
                footer_links_html += f'<div><h4>{title}</h4><ul>{items_html}</ul></div>'
    if not footer_links_html:
        footer_links_html = '<div><h4>Quick Links</h4><ul><li><a href="#top">Home</a></li><li><a href="#cta">Contact</a></li></ul></div>'

    return (
        '<!doctype html>'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="theme-color" content="{_esc(content.get("theme_color", "#173f35"))}">'
        f'<title>{brand}</title>'
        '<link rel="stylesheet" href="styles.css"></head>'
        '<body>'
        '<header class="topbar">'
        f'<a class="brand" href="#top">{brand}</a>'
        f'<nav class="nav-links">{nav_links_html}</nav>'
        '<button class="mobile-toggle" aria-label="Toggle navigation"><span></span><span></span><span></span></button>'
        '</header>'
        '<main id="top">'
        '<section class="hero">'
        '<div class="hero-copy">'
        f'<p class="eyebrow">{hero_eyebrow}</p>'
        f'<h1>{hero_headline}<br><span>{hero_accent}</span></h1>'
        f'<p class="lead">{hero_lead}</p>'
        f'<a class="button" href="{hero_cta_link}">{hero_cta} <span aria-hidden="true">\u2192</span></a>'
        f'{hero_note_html}'
        '</div>'
        f'<div class="hero-art" aria-label="{art_label}" role="img">'
        f'{illustration_shapes}'
        '</div>'
        '</section>'
        '<section class="intro">'
        f'<p class="eyebrow">{intro_eyebrow}</p>'
        f'<h2>{intro_heading}</h2>'
        f'<p>{intro_text}</p>'
        '</section>'
        f'<section class="features" aria-label="Key highlights">{features_html}</section>'
        f'<section id="cta" class="admissions">'
        f'<div><p class="eyebrow">{cta_eyebrow}</p>'
        f'<h2>{cta_heading}</h2>'
        f'<p>{cta_text}</p></div>'
        f'<a class="button button-light" href="{cta_link}">{cta_button} <span aria-hidden="true">\u2192</span></a>'
        '</section>'
        f'<footer id="contact">'
        f'<div class="footer-grid">'
        f'<div><p><strong>{footer_brand}</strong></p><p>{footer_desc}</p></div>'
        f'{footer_links_html}'
        '</div>'
        '</footer>'
        '</main>'
        '<script src="app.js" defer></script>'
        '</body></html>'
    )


def _build_portfolio_html(content: dict[str, Any], illustration_shapes: str) -> str:
    brand = _esc(content.get("brand_name", "Portfolio"))
    hero_eyebrow = _esc(content.get("hero_eyebrow", "WELCOME"))
    hero_headline = _esc(content.get("hero_headline", "Creative Work"))
    hero_accent = _esc(content.get("hero_headline_accent", "that speaks"))
    hero_lead = _esc(content.get("hero_lead", "Showcasing projects and creative endeavors."))
    hero_cta = _esc(content.get("hero_cta_text", "View Work"))
    art_label = _esc(content.get("hero_art_label", "Decorative illustration"))

    intro_eyebrow = _esc(content.get("intro_eyebrow", "ABOUT"))
    intro_heading = _esc(content.get("intro_heading", "About me"))
    intro_text = _esc(content.get("intro_text", "A passionate creator building meaningful experiences."))

    projects = content.get("features", [])
    if not isinstance(projects, list) or len(projects) < 1:
        projects = [
            {"title": "Project Alpha", "description": "A creative exploration of form and function."},
            {"title": "Project Beta", "description": "Pushing boundaries with innovative design."},
            {"title": "Project Gamma", "description": "Where art meets technology."},
        ]
    projects_html = ""
    for i, proj in enumerate(projects[:6]):
        if isinstance(proj, dict):
            num = f"{i + 1:02d}"
            title = _esc(str(proj.get("title", f"Project {i+1}")))
            desc = _esc(str(proj.get("description", "")))
            projects_html += f'<article><span class="icon">{num}</span><h3>{title}</h3><p>{desc}</p></article>'

    cta_eyebrow = _esc(content.get("cta_eyebrow", "GET IN TOUCH"))
    cta_heading = _esc(content.get("cta_heading", "Let's work together"))
    cta_text = _esc(content.get("cta_text", "Have a project in mind? Reach out."))
    cta_button = _esc(content.get("cta_button_text", "Say Hello"))

    footer_desc = _esc(content.get("footer_text", "Available for freelance projects and collaborations."))

    return (
        '<!doctype html>'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{brand}</title>'
        '<link rel="stylesheet" href="styles.css"></head>'
        '<body>'
        '<header class="topbar">'
        f'<a class="brand" href="#top">{brand}</a>'
        '<nav class="nav-links"><a href="#work">Work</a><a href="#about">About</a><a class="nav-cta" href="#contact">Contact</a></nav>'
        '<button class="mobile-toggle" aria-label="Toggle navigation"><span></span><span></span><span></span></button>'
        '</header>'
        '<main id="top">'
        '<section class="hero">'
        '<div class="hero-copy">'
        f'<p class="eyebrow">{hero_eyebrow}</p>'
        f'<h1>{hero_headline}<br><span>{hero_accent}</span></h1>'
        f'<p class="lead">{hero_lead}</p>'
        f'<a class="button" href="#work">{hero_cta} <span aria-hidden="true">\u2192</span></a>'
        '</div>'
        f'<div class="hero-art" aria-label="{art_label}" role="img">{illustration_shapes}</div>'
        '</section>'
        f'<section id="about" class="intro">'
        f'<p class="eyebrow">{intro_eyebrow}</p>'
        f'<h2>{intro_heading}</h2>'
        f'<p>{intro_text}</p>'
        '</section>'
        f'<section id="work" class="features" aria-label="Selected work">{projects_html}</section>'
        f'<section id="contact" class="admissions">'
        f'<div><p class="eyebrow">{cta_eyebrow}</p><h2>{cta_heading}</h2><p>{cta_text}</p></div>'
        f'<a class="button button-light" href="mailto:hello@example.com">{cta_button} <span aria-hidden="true">\u2192</span></a>'
        '</section>'
        f'<footer><p><strong>{brand}</strong></p><p>{footer_desc}</p></footer>'
        '</main>'
        '<script src="app.js" defer></script>'
        '</body></html>'
    )


def _build_restaurant_html(content: dict[str, Any], illustration_shapes: str) -> str:
    brand = _esc(content.get("brand_name", "Restaurant"))
    hero_eyebrow = _esc(content.get("hero_eyebrow", "WELCOME"))
    hero_headline = _esc(content.get("hero_headline", "Fine Dining"))
    hero_accent = _esc(content.get("hero_headline_accent", "experience"))
    hero_lead = _esc(content.get("hero_lead", "Savor every moment with exceptional cuisine."))
    hero_cta = _esc(content.get("hero_cta_text", "Reserve a Table"))
    art_label = _esc(content.get("hero_art_label", "Decorative illustration"))

    intro_eyebrow = _esc(content.get("intro_eyebrow", "OUR STORY"))
    intro_heading = _esc(content.get("intro_heading", "Crafted with care"))
    intro_text = _esc(content.get("intro_text", "Every dish tells a story of passion and tradition."))

    features = content.get("features", [])
    if not isinstance(features, list) or len(features) < 1:
        features = [
            {"title": "Fresh Ingredients", "description": "Locally sourced, seasonal produce in every dish."},
            {"title": "Expert Chefs", "description": "Culinary artists with decades of experience."},
            {"title": "Warm Atmosphere", "description": "A welcoming space designed for memorable moments."},
        ]
    features_html = ""
    for i, feat in enumerate(features[:6]):
        if isinstance(feat, dict):
            num = f"{i + 1:02d}"
            title = _esc(str(feat.get("title", f"Feature {i+1}")))
            desc = _esc(str(feat.get("description", "")))
            features_html += f'<article><span class="icon">{num}</span><h3>{title}</h3><p>{desc}</p></article>'

    cta_eyebrow = _esc(content.get("cta_eyebrow", "RESERVE"))
    cta_heading = _esc(content.get("cta_heading", "Book your table"))
    cta_text = _esc(content.get("cta_text", "Join us for an unforgettable dining experience."))
    cta_button = _esc(content.get("cta_button_text", "Make a Reservation"))

    footer_desc = _esc(content.get("footer_text", "Visit us or call to reserve."))

    return (
        '<!doctype html>'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{brand}</title>'
        '<link rel="stylesheet" href="styles.css"></head>'
        '<body>'
        '<header class="topbar">'
        f'<a class="brand" href="#top">{brand}</a>'
        '<nav class="nav-links"><a href="#menu">Menu</a><a href="#about">About</a><a class="nav-cta" href="#reserve">Reserve</a></nav>'
        '<button class="mobile-toggle" aria-label="Toggle navigation"><span></span><span></span><span></span></button>'
        '</header>'
        '<main id="top">'
        '<section class="hero">'
        '<div class="hero-copy">'
        f'<p class="eyebrow">{hero_eyebrow}</p>'
        f'<h1>{hero_headline}<br><span>{hero_accent}</span></h1>'
        f'<p class="lead">{hero_lead}</p>'
        f'<a class="button" href="#reserve">{hero_cta} <span aria-hidden="true">\u2192</span></a>'
        '</div>'
        f'<div class="hero-art" aria-label="{art_label}" role="img">{illustration_shapes}</div>'
        '</section>'
        f'<section id="about" class="intro">'
        f'<p class="eyebrow">{intro_eyebrow}</p>'
        f'<h2>{intro_heading}</h2>'
        f'<p>{intro_text}</p>'
        '</section>'
        f'<section id="menu" class="features" aria-label="Highlights">{features_html}</section>'
        f'<section id="reserve" class="admissions">'
        f'<div><p class="eyebrow">{cta_eyebrow}</p><h2>{cta_heading}</h2><p>{cta_text}</p></div>'
        f'<a class="button button-light" href="tel:+1234567890">{cta_button} <span aria-hidden="true">\u2192</span></a>'
        '</section>'
        f'<footer><p><strong>{brand}</strong></p><p>{footer_desc}</p></footer>'
        '</main>'
        '<script src="app.js" defer></script>'
        '</body></html>'
    )


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

APP_JS = """'use strict';
document.querySelectorAll('a[href^="#"]').forEach(function(link) {
  link.addEventListener('click', function(event) {
    var target = document.querySelector(link.getAttribute('href'));
    if (target) { event.preventDefault(); target.scrollIntoView({ behavior: 'smooth' }); }
  });
});
(function() {
  var toggle = document.querySelector('.mobile-toggle');
  var nav = document.querySelector('.nav-links');
  if (toggle && nav) {
    toggle.addEventListener('click', function() {
      nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', nav.classList.contains('open'));
    });
  }
})();
document.querySelectorAll('img').forEach(function(img) {
  img.addEventListener('error', function() {
    img.style.display = 'none';
  });
});
"""


# ---------------------------------------------------------------------------
# Content prompt for the LLM (much smaller than full code generation)
# ---------------------------------------------------------------------------

def build_content_prompt(request: str, layout: str) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) that asks the LLM for content only."""

    system = (
        "You are a professional copywriter. Given a website request, generate ONLY the text content as JSON. "
        "Do NOT generate any HTML, CSS, or JavaScript code. Return ONLY valid JSON matching this exact schema:\n"
        '{"brand_name": "string", '
        '"hero_eyebrow": "short uppercase label", '
        '"hero_headline": "main headline (4-8 words)", '
        '"hero_headline_accent": "accent part of headline (2-5 words)", '
        '"hero_lead": "one sentence description", '
        '"hero_cta_text": "button text (2-4 words)", '
        '"hero_cta_link": "#section-id", '
        '"hero_date_note": "optional short note or empty string", '
        '"hero_art_label": "description of illustration for accessibility", '
        '"intro_eyebrow": "SHORT UPPERCASE LABEL", '
        '"intro_heading": "section heading (3-8 words)", '
        '"intro_text": "2-3 sentences about the topic", '
        '"features": [{"title": "short title", "description": "one sentence"}], '
        '"cta_eyebrow": "SHORT UPPERCASE LABEL", '
        '"cta_heading": "call to action heading", '
        '"cta_text": "one sentence with specific details", '
        '"cta_button_text": "button text (2-4 words)", '
        '"cta_link": "#contact", '
        '"footer_text": "one sentence", '
        '"theme": "default|ocean|warm|cool|dark", '
        '"illustration": "books|geometric|waves|abstract"}\n\n'
        "Rules:\n"
        "- Write compelling, specific copy tailored to the brand/industry\n"
        "- Provide exactly 3 features/highlights unless the request implies more\n"
        "- Choose the theme and illustration style that best fits the brand\n"
        "- Do NOT use generic placeholder text\n"
        "- Do NOT include any HTML tags\n"
        "- Keep all text concise and professional\n"
        "- Return ONLY the JSON object, no markdown fences or explanation"
    )
    user = f"Generate professional website content for: {request}"
    return system, user


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_website(content: dict[str, Any], request: str = "") -> dict[str, str]:
    """Assemble a complete professional website from content data.

    Returns a dict of {filename: file_content} ready to write to disk.
    """
    theme_name = str(content.get("theme", "")) or _detect_theme(request)
    illustration_name = str(content.get("illustration", "")) or _detect_illustration(request)
    layout = _detect_layout(request)

    theme = _select_theme(theme_name)
    illustration = _select_illustration(illustration_name)

    css = _build_css(theme, illustration["css"])

    builders = {
        "landing": _build_landing_html,
        "portfolio": _build_portfolio_html,
        "restaurant": _build_restaurant_html,
    }
    builder = builders.get(layout, _build_landing_html)
    html = builder(content, illustration["shapes"])

    readme_lines = [
        f"# {content.get('brand_name', 'Website')}",
        "",
        "A professional website generated by EVA.",
        "",
        "## How to view",
        "",
        "Open `index.html` in any modern web browser.",
        "",
        "## Design details",
        "",
        f"- Layout: {layout}",
        f"- Theme: {theme_name}",
        f"- Illustration: {illustration_name}",
        "- Responsive: mobile-first with breakpoint at 760px",
        "- Typography: Georgia serif for headings, system sans-serif for body",
        "- CSS illustrations used instead of external images for reliability",
        "- Accessible: semantic HTML, focus states, reduced-motion support",
        "",
        "## Files",
        "",
        "- `index.html` — main page",
        "- `styles.css` — all styling with CSS custom properties",
        "- `app.js` — smooth scroll, mobile navigation, image error handling",
    ]

    return {
        "index.html": html,
        "styles.css": css,
        "app.js": APP_JS.strip(),
        "README.md": "\n".join(readme_lines) + "\n",
    }


def is_website_request(request: str) -> bool:
    """Check if a request is for a website that the engine can handle."""
    return bool(re.search(
        r"\b(website|web site|landing page|portfolio|homepage|webpage|web page|"
        r"site design|portal|school website|restaurant website|business website|"
        r"personal site|company site|cafe website|bakery website)\b",
        request, re.I
    ))
