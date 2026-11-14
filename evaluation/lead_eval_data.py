"""
lead_eval_data.py — a reproducible, hand-written evaluation set for the lead engine (SYNTHETIC; all of it written by the same author as the
engine, so it measures behaviour on these phrasings, not real-world accuracy).

6 business profiles x (16 positive, 8 negative, 3 ambiguous, 2 historical) items, plus 6 non-English positives. Every item has an EXPECTED surface:
  lead    a current, genuine customer opportunity: must surface as "High potential" or "Relevant"
  review  plausible but not provable (ambiguous, or historical): may be "Needs review", must NOT be High/Relevant
  none    not a lead: must not be shown at all
`split` is "dev" or "held" (alternating within each business and kind). During development only the dev split is inspected; the held split is
scored once per milestone. The first 60 items per the old audit matrix are flagged `baseline` so the previous 17/36 and 1/24 can be reproduced.
Nothing here is tuned to a threshold: change the engine, never the labels.
"""

BUSINESSES = {
    "shopify": dict(business_name="ShopCraft", sells="Shopify development services for online stores", ideal_customers="ecommerce store owners, DTC brands",
                    problems_solved="slow store, checkout drop-off, low conversion, theme customization, migration to Shopify",
                    competitors="Webflow, WooCommerce", product_terms="Shopify developer, Shopify Entwickler"),
    "saas": dict(business_name="HireFlow", sells="applicant tracking software for small companies", ideal_customers="small business owners, HR managers",
                 problems_solved="managing job applicants, scheduling interviews, spreadsheets for hiring", competitors="Greenhouse, Lever"),
    "marketing": dict(business_name="GrowthLab", sells="social media marketing services for small businesses", ideal_customers="small business owners, local shops",
                      problems_solved="not enough customers, low engagement on Instagram, no time for social media"),
    "designer": dict(business_name="Mira Design", sells="logo and brand identity design for startups", ideal_customers="startup founders",
                     problems_solved="looking unprofessional, no brand identity, inconsistent branding"),
    "plumber": dict(business_name="Leeds Plumbing Co", sells="plumbing and boiler repair services", ideal_customers="homeowners, landlords",
                    problems_solved="leaking pipes, broken boilers, blocked drains", geo_scope="city", geo_places="Leeds", product_terms="fontanero"),
    "skincare": dict(business_name="DermaGlow", sells="skincare products for dry and sensitive skin", ideal_customers="people with dry skin, sensitive skin",
                     problems_solved="dry skin, irritation, eczema flare ups", competitors="CeraVe", product_terms="soin de la peau, peau sèche"),
}

_ITEMS = []


def P(biz, kind, expect, title, body, days=3, lang="en", baseline=False, fresh=False):
    _ITEMS.append(dict(biz=biz, kind=kind, expect=expect, title=title, body=body, days=days, lang=lang, baseline=baseline, fresh=fresh))


# ═══ Shopify development agency ═══════════════════════════════════════════════════════════════════════════════════════════
P("shopify", "direct_request", "lead", "Need a Shopify developer", "I run an online store and need a Shopify developer to customize our theme. Budget is $3k.", baseline=True)
P("shopify", "direct_request", "lead", "Looking for someone to build our store", "We sell candles online and want a developer who knows Shopify to build a custom product page. Who should I talk to?")
P("shopify", "recommendation", "lead", "Shopify agency recommendations?", "Can anyone recommend a good Shopify agency for a store rebuild?", baseline=True)
P("shopify", "recommendation", "lead", "Any good Shopify experts?", "Our store needs help with a custom checkout. Any Shopify experts you'd recommend?")
P("shopify", "problem", "lead", "Our Shopify checkout is losing customers", "Our checkout drop-off is terrible and the store is so slow on mobile. We don't know how to fix our Shopify store.", baseline=True)
P("shopify", "problem", "lead", "Conversion rate stuck at 0.8%", "Our online store gets traffic but our conversion is terrible and people abandon at checkout. No idea what is wrong.")
P("shopify", "provider_search", "lead", "Who can migrate WooCommerce to Shopify?", "We want to move our WooCommerce shop to Shopify. Who can do this migration for us?", baseline=True)
P("shopify", "provider_search", "lead", "Hiring: Shopify theme customization", "Looking to hire a freelancer or agency to customize our Shopify theme and speed up the store.")
P("shopify", "comparison", "lead", "Shopify vs WooCommerce for a 200 SKU store", "We're comparing Shopify and WooCommerce for our online store and want to hire someone to set it up. Thoughts?", baseline=True)
P("shopify", "comparison", "lead", "Webflow or Shopify for my brand?", "Deciding between Webflow and Shopify for our DTC brand. If we go Shopify we'll need a developer. What do people recommend?")
P("shopify", "buying_question", "lead", "How much does a Shopify store build cost?", "We are a DTC brand planning a new Shopify store. How much should we budget to hire an agency to build it?", baseline=True)
P("shopify", "buying_question", "lead", "What do Shopify developers charge?", "How much does a Shopify developer charge per hour? I need some custom work on my store.")
P("shopify", "replace_provider", "lead", "Our Shopify agency keeps missing deadlines", "We're considering switching from our current Shopify agency. They keep missing deadlines. Who else is good?")
P("shopify", "replace_provider", "lead", "Need to replace our store developer", "Our developer left and we need a new Shopify developer to take over the theme code.")
P("shopify", "indirect", "lead", "Store redesign time", "My brand's online store looks dated. Planning a full redesign on Shopify this quarter and I'd like to find a team to do it.")
P("shopify", "indirect", "lead", "Page speed score is 22", "Lighthouse says our Shopify store scores 22 on mobile. I can't fix this myself and need help.")
P("shopify", "definition", "none", "What is Shopify?", "Can someone explain what Shopify is and how it works in general?", baseline=True)
P("shopify", "vendor_promo", "none", "We build Shopify stores", "We are a Shopify development agency. We build fast stores for ecommerce brands. DM us for a free audit.", baseline=True)
P("shopify", "offtopic_lookalike", "none", "Shop floor safety", "We need a safety officer for our factory shop floor. Looking for a recommendation.", baseline=True)
P("shopify", "chat", "none", "Weekend thread", "How was everyone's weekend? I went hiking.", baseline=True)
P("shopify", "news", "none", "Shopify announces Q3 earnings", "Shopify reported quarterly revenue growth today and analysts expect further growth in ecommerce.")
P("shopify", "validation_probe", "none", "Would merchants pay for a Shopify speed tool?", "Is anyone actually looking for a Shopify speed optimisation tool? I'm thinking of building one and validating the idea.")
P("shopify", "offering_services", "none", "Freelance Shopify dev available", "I'm a freelance Shopify developer with 6 years of experience, available for new projects. Portfolio in comments.")
P("shopify", "tutorial", "none", "How I sped up my Shopify theme", "I wrote a guide on how I improved my own Shopify store's page speed by lazy loading images. Sharing the steps.")
P("shopify", "ambiguous", "review", "Thinking about selling online", "I might start selling my crafts online someday. Not sure about the platform yet.")
P("shopify", "ambiguous", "review", "Is Shopify worth it?", "Is Shopify worth it for small shops? Curious what people think.")
P("shopify", "ambiguous", "review", "Store slow?", "Does anyone else find online stores slow on mobile these days?")
P("shopify", "historical", "review", "Need a Shopify developer", "I run an online store and need a Shopify developer to customize our theme. Budget is $3k.", days=320)
P("shopify", "historical", "review", "Which agency for a Shopify rebuild?", "Can anyone recommend a good agency for a Shopify store rebuild?", days=400)
P("shopify", "non_english", "lead", "Suche Shopify Entwickler", "Wir suchen einen Shopify Entwickler für unseren Online-Shop. Kann jemand jemanden empfehlen?", lang="de")
P("shopify", "non_english", "lead", "Shopify Entwickler gesucht für Theme", "Ich brauche einen Shopify Entwickler, der unser Theme anpasst. Budget vorhanden.", lang="de")

# ═══ SaaS company (applicant tracking software) ═══════════════════════════════════════════════════════════════════════════
P("saas", "direct_request", "lead", "Need an ATS", "We are a 20 person company and need an applicant tracking system. Budget is $200/month.", baseline=True)
P("saas", "direct_request", "lead", "Looking for hiring software", "I'm the only HR person at a 40 person company and I'm looking for software to manage job applicants.")
P("saas", "recommendation", "lead", "ATS recommendations", "Any recommendations for an applicant tracking tool for a small business?", baseline=True)
P("saas", "recommendation", "lead", "What do you use to track candidates?", "What tool do you all use to track candidates? Small team, tight budget. Recommendations welcome.")
P("saas", "problem", "lead", "Hiring in spreadsheets is chaos", "We manage all job applicants in spreadsheets and keep losing track of interviews. It is a mess, help.", baseline=True)
P("saas", "problem", "lead", "Interview scheduling is eating my week", "Scheduling interviews takes me hours every week because we have no system for hiring. There has to be a better way.")
P("saas", "provider_search", "lead", "Looking for hiring software vendor", "Which vendor should we talk to for applicant tracking software? We have 15 employees.", baseline=True)
P("saas", "provider_search", "lead", "Who sells recruiting software for small teams?", "Who offers applicant tracking software that isn't built for enterprises? Need something simple.")
P("saas", "comparison", "lead", "Greenhouse alternatives", "Greenhouse is too expensive for us. What are alternatives to Greenhouse for a small company?", baseline=True)
P("saas", "comparison", "lead", "Lever vs a simpler tool", "We use Lever but it feels heavy for 12 employees. What's a lighter applicant tracking option?")
P("saas", "buying_question", "lead", "Is paying for an ATS worth it?", "Is it worth paying for an applicant tracking system when you hire 5 people a year?", baseline=True)
P("saas", "buying_question", "lead", "ATS pricing for a small company", "How much does applicant tracking software usually cost for a 25 person company?")
P("saas", "replace_provider", "lead", "We're considering switching from our current ATS", "We're considering switching from our current applicant tracking system. It's slow and support is poor.")
P("saas", "replace_provider", "lead", "Replace our hiring spreadsheet and old tool", "We need to replace our old applicant tracker. What should a 30 person company move to?")
P("saas", "indirect", "lead", "Offer letters getting lost", "Last week we lost two candidates because offer emails got buried. We really need to organize hiring somehow.")
P("saas", "indirect", "lead", "Growing fast, hiring 10 people", "We're growing fast and about to hire 10 people this quarter with nothing but email and a shared sheet. Advice on how to handle applicants?")
P("saas", "definition", "none", "What is an ATS?", "What does ATS stand for and how does applicant tracking software work?", baseline=True)
P("saas", "vendor_promo", "none", "Show HN: TrackHire", "I built TrackHire, applicant tracking software for small companies. Free trial, feedback welcome.", baseline=True)
P("saas", "offtopic_lookalike", "none", "Track my package", "Looking for software to track my package deliveries.", baseline=True)
P("saas", "chat", "none", "Interview tips", "Share your best interview tips for candidates.", baseline=True)
P("saas", "news", "none", "Unemployment figures released", "Job openings fell last month according to the latest labour statistics.")
P("saas", "validation_probe", "none", "Would HR teams pay for this?", "Would small companies pay for an applicant tracking tool? Trying to validate my startup idea.")
P("saas", "job_seeker", "none", "Looking for a job in HR", "I'm looking for an HR coordinator job. Any companies hiring? Happy to share my resume.")
P("saas", "tutorial", "none", "How to write a job post", "Here is how I write job postings that attract applicants: be specific about the role.")
P("saas", "ambiguous", "review", "HR is hard", "Being the only HR person at a small company is hard sometimes.")
P("saas", "ambiguous", "review", "Software for small teams", "What software do small teams use? Just curious about the landscape.")
P("saas", "ambiguous", "review", "Hiring season", "Hiring season again. Anyone else swamped?")
P("saas", "historical", "review", "Need an ATS", "We are a 20 person company and need an applicant tracking system. Budget is $200/month.", days=310)
P("saas", "historical", "review", "Greenhouse alternatives", "Greenhouse is too expensive for us. What are alternatives for a small company?", days=380)

# ═══ Marketing agency ═════════════════════════════════════════════════════════════════════════════════════════════════════
P("marketing", "direct_request", "lead", "Need social media manager", "We run a bakery and need someone to manage our Instagram and social media marketing. Budget is $500/month.", baseline=True)
P("marketing", "direct_request", "lead", "Looking for help with our social media", "I own a small gym and I'm looking for someone to take over our social media. Not enough time.")
P("marketing", "recommendation", "lead", "Marketing agency recommendations", "Can anyone recommend a marketing agency for a small business?", baseline=True)
P("marketing", "recommendation", "lead", "Who do you use for social ads?", "Small shop owner here. Can you recommend a marketing agency or freelancer who handles social media for local businesses?")
P("marketing", "problem", "lead", "Instagram engagement is dead", "My shop's Instagram has almost no engagement and we can't get new customers. We don't have time for social media.", baseline=True)
P("marketing", "problem", "lead", "Nobody finds our restaurant online", "We're a small restaurant and we get hardly any new customers. Our social media accounts just sit there with no followers.")
P("marketing", "provider_search", "lead", "Who runs social media for small businesses?", "Where can I hire someone to run social media marketing for my small business?", baseline=True)
P("marketing", "provider_search", "lead", "Looking for a marketing partner", "We're a boutique looking for a marketing agency that understands small local businesses.")
P("marketing", "comparison", "lead", "Agency or freelancer for social media?", "We're debating hiring a social media marketing agency versus a freelancer for our shop. Opinions?", baseline=True)
P("marketing", "comparison", "lead", "In-house or outsource social?", "Should a 5 person business outsource social media marketing or hire in-house? Looking for advice and providers.")
P("marketing", "buying_question", "lead", "What do social media agencies charge?", "What should a small business expect to pay for social media marketing services?", baseline=True)
P("marketing", "buying_question", "lead", "Marketing retainer price?", "How much does a marketing agency charge monthly for a small business with about 2k followers?")
P("marketing", "replace_provider", "lead", "Unhappy with our marketing agency", "We're thinking of switching from our current marketing agency. Results have been flat for six months.")
P("marketing", "replace_provider", "lead", "Our social media freelancer quit", "Our social media freelancer quit and we need someone new to run our accounts.")
P("marketing", "indirect", "lead", "Posting is a chore", "I keep forgetting to post for my shop on Instagram and sales are down. Wish someone could just handle it.")
P("marketing", "indirect", "lead", "Competitors have more followers", "All the other cafes in town have thousands of followers and we have 300. How do we catch up? We'd pay for help.")
P("marketing", "definition", "none", "What is social media marketing?", "Can someone explain what social media marketing is?", baseline=True)
P("marketing", "vendor_promo", "none", "Our agency offers social media marketing", "We are a marketing agency offering social media marketing for small businesses. DM for rates.", baseline=True)
P("marketing", "offtopic_lookalike", "none", "Media coverage", "Looking for a PR firm to get media coverage for our startup funding round.", baseline=True)
P("marketing", "chat", "none", "Social media addiction", "Do you think social media is making us lonely?", baseline=True)
P("marketing", "news", "none", "Instagram changes its algorithm again", "Instagram announced another algorithm change today. Reach for small accounts is expected to shift.")
P("marketing", "validation_probe", "none", "Is anyone paying for Instagram automation?", "Would small shops actually pay for an Instagram scheduling tool? Validating an idea.")
P("marketing", "offering_services", "none", "Social media manager available", "I'm a freelance social media manager taking on two new clients. Rates in my profile.")
P("marketing", "tutorial", "none", "How I grew my page to 10k", "Here is how I grew my own Instagram page to 10k followers with daily reels.")
P("marketing", "ambiguous", "review", "Instagram thoughts", "Instagram feels different these days. Anyone else notice?")
P("marketing", "ambiguous", "review", "Small business tips", "What are your general tips for running a small business?")
P("marketing", "ambiguous", "review", "Marketing budget", "How do you decide on a marketing budget? Just curious.")
P("marketing", "historical", "review", "Need social media manager", "We run a bakery and need someone to manage our Instagram. Budget is $500/month.", days=330)
P("marketing", "historical", "review", "Marketing agency recommendations", "Can anyone recommend a marketing agency for a small business?", days=420)

# ═══ Freelance designer ═══════════════════════════════════════════════════════════════════════════════════════════════════
P("designer", "direct_request", "lead", "Need a logo designer", "I'm a startup founder and need a logo designed for my new company. Budget is $400.", baseline=True)
P("designer", "direct_request", "lead", "Looking for a designer for our brand", "We're a seed-stage startup looking for a designer to create our brand identity before launch.")
P("designer", "recommendation", "lead", "Logo designer recommendations", "Can anyone recommend a brand identity designer for a startup?", baseline=True)
P("designer", "recommendation", "lead", "Designer you'd trust with a rebrand?", "Founders of this sub: who would you recommend for logo and branding work for an early startup?")
P("designer", "problem", "lead", "Our branding looks amateur", "Our startup's branding looks unprofessional and inconsistent and we don't know how to fix it.", baseline=True)
P("designer", "problem", "lead", "Investors said our deck looks cheap", "Two investors told us our startup looks unprofessional. We have no real brand identity, just a free logo maker output.")
P("designer", "provider_search", "lead", "Where to find a brand designer?", "Where can I hire a brand identity designer for my startup?", baseline=True)
P("designer", "provider_search", "lead", "Who designs logos for startups?", "Who can design a logo and brand guidelines for a new SaaS startup? Hiring this month.")
P("designer", "comparison", "lead", "99designs or a freelancer?", "Should I use 99designs or hire a freelance logo designer for my startup brand?", baseline=True)
P("designer", "comparison", "lead", "Agency vs freelancer for branding", "Branding agency or independent designer for a pre-seed startup? We need a logo and identity.")
P("designer", "buying_question", "lead", "How much for a brand identity?", "How much should a startup budget for logo and brand identity design?", baseline=True)
P("designer", "buying_question", "lead", "What does a logo designer cost?", "What do freelance designers usually charge for a logo? I'm launching a startup soon.")
P("designer", "replace_provider", "lead", "Our designer isn't working out", "We're thinking of replacing our current brand designer. The logo variations all feel off.")
P("designer", "replace_provider", "lead", "Need a new designer for our brand", "The freelancer who made our logo has gone quiet and we need a new designer to finish the brand identity.")
P("designer", "indirect", "lead", "Launching in 6 weeks with no brand", "Launching our startup in six weeks and we still have no logo or visual identity. Panic mode.")
P("designer", "indirect", "lead", "Our website and deck look inconsistent", "Every asset we make for our startup looks different. Fonts, colors, all over the place. Need to sort this out soon.")
P("designer", "definition", "none", "What is a brand identity?", "What does brand identity mean and why does it matter?", baseline=True)
P("designer", "vendor_promo", "none", "Freelance logo designer available", "I'm a freelance designer offering logo and brand identity design for startups. Portfolio in comments.", baseline=True)
P("designer", "offtopic_lookalike", "none", "Logo of the football club", "Does anyone know who designed the logo of the Arsenal football club?", baseline=True)
P("designer", "chat", "none", "Design inspiration", "Share your favourite designs of the year.", baseline=True)
P("designer", "news", "none", "Company unveils new logo", "A large airline unveiled a new logo today, and the internet has opinions about the design.")
P("designer", "validation_probe", "none", "Would founders pay for AI logos?", "Would startup founders pay for an AI logo generator? Trying to validate an idea.")
P("designer", "job_seeker", "none", "Looking for a design job", "Recent graduate looking for a junior graphic design job. Open to remote.")
P("designer", "tutorial", "none", "How to pick brand colors", "A short guide on picking brand colors for your own side project.")
P("designer", "ambiguous", "review", "Fonts", "What fonts do you like? Just browsing.")
P("designer", "ambiguous", "review", "Startup branding", "Branding seems important for startups. Any general thoughts?")
P("designer", "ambiguous", "review", "Thinking about a name", "We're picking a name for our startup this month.")
P("designer", "historical", "review", "Need a logo designer", "I'm a startup founder and need a logo designed for my new company. Budget is $400.", days=340)
P("designer", "historical", "review", "Logo designer recommendations", "Can anyone recommend a brand identity designer for a startup?", days=390)

# ═══ Local plumbing business (Leeds) ══════════════════════════════════════════════════════════════════════════════════════
P("plumber", "direct_request", "lead", "Need a plumber in Leeds", "I need a plumber in Leeds for a leaking pipe under my sink.", baseline=True)
P("plumber", "direct_request", "lead", "Boiler broke, need an engineer", "Our boiler stopped working this morning in Leeds and we need someone out today.")
P("plumber", "recommendation", "lead", "Boiler repair recommendations Leeds", "Can anyone recommend a good boiler repair service in Leeds?", baseline=True)
P("plumber", "recommendation", "lead", "Anyone know a reliable plumber near Headingley?", "Any recommendations for a reliable plumber in Leeds? Need to fix a dripping tap and a radiator.")
P("plumber", "problem", "lead", "Blocked drain again", "My drain in Leeds is blocked again and the water keeps backing up. Any ideas who to call?", baseline=True)
P("plumber", "problem", "lead", "Water coming through the ceiling", "There is water coming through the bathroom ceiling in our Leeds flat and I think a pipe has burst.")
P("plumber", "provider_search", "lead", "Plumber near Headingley", "Who can fix a leaking pipe in Leeds this week? Looking for a plumbing service.", baseline=True)
P("plumber", "provider_search", "lead", "Landlord looking for a heating engineer", "I'm a landlord with three flats in Leeds and I'm looking for a plumber to service the boilers every year.")
P("plumber", "comparison", "lead", "Gas Safe plumber vs handyman", "Should I hire a plumber or a handyman for a leaking pipe at my Leeds rental?", baseline=True)
P("plumber", "comparison", "lead", "Boiler repair or replace?", "Should I repair or replace my old boiler in Leeds? Looking for a plumber to give an honest opinion.")
P("plumber", "buying_question", "lead", "How much is a boiler repair?", "How much does boiler repair cost in Leeds? I need a plumber soon.", baseline=True)
P("plumber", "buying_question", "lead", "What do plumbers charge per hour?", "What does a plumber charge per hour in Leeds? Have a blocked toilet.")
P("plumber", "replace_provider", "lead", "Our usual plumber retired", "Our usual plumber in Leeds retired and we need to find a new one for our rental properties.")
P("plumber", "replace_provider", "lead", "Unhappy with plumber who did our bathroom", "We're switching from our current plumber after the second leak. Who do people use in Leeds?")
P("plumber", "indirect", "lead", "Cold radiators all winter", "Half the radiators in our Leeds house are cold and the heating takes forever. Need to get this sorted before it freezes.")
P("plumber", "indirect", "lead", "Smell from the drain", "A bad smell keeps coming from the kitchen drain in our Leeds home and nothing I try helps.")
P("plumber", "definition", "none", "How do plumbers work?", "Can someone explain how a plumbing system works in general?", baseline=True)
P("plumber", "vendor_promo", "none", "Leeds plumber offering", "We are a plumbing company in Leeds offering boiler repair. Call us.", baseline=True)
P("plumber", "offtopic_lookalike", "none", "Need a plumber in Sydney", "I need a plumber in Sydney for a leaking pipe under my sink.", baseline=True)
P("plumber", "chat", "none", "Leeds weather", "Is it always raining in Leeds?", baseline=True)
P("plumber", "news", "none", "Water company fined", "A water company was fined for leaking pipes across the region.")
P("plumber", "validation_probe", "none", "Would plumbers pay for a booking app?", "Would plumbers actually pay for an online booking app? Validating an idea.")
P("plumber", "offering_services", "none", "Plumber available weekends", "Qualified plumber in Leeds with 10 years of experience, available at weekends. Message me.")
P("plumber", "tutorial", "none", "How I fixed my own tap", "Here is how I replaced a washer in my kitchen tap in 20 minutes, step by step.")
P("plumber", "ambiguous", "review", "Old house problems", "Old houses always have something wrong with them. Plumbing, wiring, you name it.")
P("plumber", "ambiguous", "review", "Is water pressure normal?", "Is low water pressure on the top floor normal in Leeds terraced houses?")
P("plumber", "ambiguous", "review", "Renovating", "We're renovating a house in Leeds next year. Lots to plan.")
P("plumber", "historical", "review", "Need a plumber in Leeds", "I need a plumber in Leeds for a leaking pipe under my sink.", days=350)
P("plumber", "historical", "review", "Boiler repair recommendations", "Can anyone recommend a boiler repair service in Leeds?", days=410)
P("plumber", "non_english", "lead", "Necesito un fontanero en Leeds", "Necesito un fontanero en Leeds para una fuga de agua en el baño. ¿Alguien recomienda uno?", lang="es")
P("plumber", "non_english", "lead", "Busco fontanero urgente Leeds", "Busco un fontanero en Leeds, se ha roto una tubería en mi piso.", lang="es")

# ═══ E-commerce skincare ══════════════════════════════════════════════════════════════════════════════════════════════════
P("skincare", "direct_request", "lead", "Need a moisturizer", "I have really dry skin and I'm looking for a good moisturizer. Willing to pay for something that works.", baseline=True)
P("skincare", "direct_request", "lead", "Looking for a gentle cleanser", "My skin is sensitive and I need a gentle cleanser that doesn't sting. Where do I buy one?")
P("skincare", "recommendation", "lead", "Sensitive skin cream recommendations", "Can anyone recommend a skincare product for sensitive skin?", baseline=True)
P("skincare", "recommendation", "lead", "Best cream for eczema flare ups?", "What would you recommend for eczema flare ups on my hands? Dry and cracked all winter.")
P("skincare", "problem", "lead", "My skin is so dry", "My skin is so dry and irritated every winter, nothing helps. Any ideas?", baseline=True)
P("skincare", "problem", "lead", "Face stings after every product", "Every product I try makes my face red and itchy. I think my skin barrier is wrecked. Help.")
P("skincare", "provider_search", "lead", "Where to buy dermatologist skincare?", "Where can I buy good skincare products for eczema prone skin?", baseline=True)
P("skincare", "provider_search", "lead", "Which brands make products for dry skin?", "Which brands make skincare for very dry, sensitive skin? I want to try something new.")
P("skincare", "comparison", "lead", "CeraVe alternatives", "CeraVe isn't working for my dry skin. What are alternatives to CeraVe?", baseline=True)
P("skincare", "comparison", "lead", "Is the pricey stuff better than drugstore?", "Drugstore moisturizer versus a premium one for dry sensitive skin, is the expensive one worth it?")
P("skincare", "buying_question", "lead", "Is expensive skincare worth it?", "Is it worth buying expensive skincare for dry sensitive skin?", baseline=True)
P("skincare", "buying_question", "lead", "How much should I spend on moisturizer?", "How much should I spend on a good moisturizer for dry skin? Not sure what's reasonable.")
P("skincare", "replace_provider", "lead", "Time to switch from CeraVe", "I'm thinking of switching from CeraVe because it irritates my skin. What should I try instead?")
P("skincare", "replace_provider", "lead", "My usual cream was discontinued", "My usual moisturizer for dry skin was discontinued and I need a replacement that is just as gentle.")
P("skincare", "indirect", "lead", "Winter is coming and my hands crack", "Every winter my hands crack and bleed from dryness. Time to find something better than the pharmacy stuff.")
P("skincare", "indirect", "lead", "Dermatologist said fragrance free", "My dermatologist said I need fragrance free products for my sensitive skin. Starting from scratch now.")
P("skincare", "definition", "none", "What is a ceramide?", "What are ceramides and how do they work in skincare?", baseline=True)
P("skincare", "vendor_promo", "none", "New skincare line", "We launched our new skincare line for dry skin. Use code GLOW for 20% off.", baseline=True)
P("skincare", "offtopic_lookalike", "none", "Dry January", "Anyone else doing dry January? Looking for good non-alcoholic drinks.", baseline=True)
P("skincare", "chat", "none", "Skin science article", "Interesting article about the history of skincare in ancient Egypt.", baseline=True)
P("skincare", "news", "none", "Cosmetics industry report", "A new market report says the skincare industry will grow next year.")
P("skincare", "validation_probe", "none", "Would people pay for custom skincare?", "Would people pay for personalised skincare? Validating my startup idea.")
P("skincare", "offering_services", "none", "Esthetician taking clients", "Licensed esthetician offering facials for dry skin, booking open.")
P("skincare", "tutorial", "none", "My nightly routine", "Here is my nightly skincare routine for dry skin: cleanse, serum, moisturize.")
P("skincare", "ambiguous", "review", "Skin stuff", "Skin changes a lot with age. Funny how that works.")
P("skincare", "ambiguous", "review", "Sunscreen debate", "Do people really apply enough sunscreen? Discussion.")
P("skincare", "ambiguous", "review", "Beauty counter visit", "Went to the beauty counter today and just looked around.")
P("skincare", "historical", "review", "Need a moisturizer", "I have really dry skin and I'm looking for a good moisturizer. Willing to pay for something that works.", days=300)
P("skincare", "historical", "review", "Cream recommendations", "Can anyone recommend a cream for sensitive skin?", days=380)
P("skincare", "non_english", "lead", "Je cherche une crème pour peau sèche", "Je cherche une bonne crème hydratante pour ma peau sèche et sensible, des recommandations ?", lang="fr")
P("skincare", "non_english", "lead", "Besoin d'un soin pour la peau sèche", "J'ai besoin d'un soin de la peau pour peau sèche, quelque chose de doux. Que me conseillez-vous ?", lang="fr")


# ═══ FRESH set: written AFTER the engine was built and tuned on `dev`; never inspected while developing. Still the same author (not independent). ═══
F = lambda *a, **k: P(*a, fresh=True, **k)
F("shopify", "direct_request", "lead", "Anyone available to fix our cart page?", "Our cart page throws errors on Shopify since the last app update and I need a developer to look at it this week.")
F("shopify", "recommendation", "lead", "Best place to find a Shopify expert?", "Starting a jewelry brand online. Where do people find a reliable Shopify expert? Suggestions appreciated.")
F("shopify", "problem", "lead", "Customers abandon at the payment step", "We get hundreds of visitors but checkout abandonment is awful, nobody completes payment on our store.")
F("shopify", "buying_question", "lead", "Price range for custom Shopify theme?", "What's a realistic price for a custom Shopify theme for a small apparel brand? Trying to plan the budget.")
F("shopify", "replace_provider", "lead", "Moving off Wix", "We're done with Wix for our online store. Looking to migrate to Shopify, who handles migrations like this?")
F("shopify", "vendor_promo", "none", "Shopify speed audits", "Our studio specialises in Shopify speed optimisation. Message us for a free audit of your store.")
F("shopify", "news", "none", "Retail sales numbers", "Online retail sales climbed last quarter according to the latest report, with mobile leading growth.")
F("shopify", "tutorial", "none", "Setting up discount codes", "Here's how I set up discount codes in my own Shopify store, step by step.")
F("saas", "direct_request", "lead", "Need to organise hiring for 8 roles", "We're opening eight positions next month and I need a proper applicant tracking tool, spreadsheets won't cut it.")
F("saas", "recommendation", "lead", "What applicant tracker do you recommend?", "Small agency, about 12 people. What applicant tracking system would you recommend for us?")
F("saas", "comparison", "lead", "Lever or Greenhouse for a startup?", "Torn between Lever and Greenhouse for our 30-person startup's recruiting. Anything simpler?")
F("saas", "problem", "lead", "Candidates ghosted us because we were slow", "We lost two great candidates because our hiring process is a mess of emails and nobody tracks where applicants are.")
F("saas", "replace_provider", "lead", "Done with our current applicant tracker", "We're fed up with our current applicant tracking system. It crashes weekly. Time to switch, suggestions?")
F("saas", "offtopic_lookalike", "none", "Parcel tracking app", "Need an app to track parcels from several couriers in one place. Recommendations?")
F("saas", "vendor_promo", "none", "Introducing HireEasy", "We launched HireEasy, an applicant tracking system for small teams. Try it free for 14 days.")
F("saas", "definition", "none", "ATS meaning", "What is an applicant tracking system and how does it work?")
F("marketing", "direct_request", "lead", "Need help with Facebook ads for my salon", "I own a hair salon and need someone to run our social media and ads, we have no time and no customers coming in.")
F("marketing", "recommendation", "lead", "Social media agency for a dentist?", "Looking for suggestions: which marketing agency would you trust to run social media for a small dental practice?")
F("marketing", "buying_question", "lead", "Typical cost of social media management?", "How much does social media management typically cost for a small business? We're comparing quotes.")
F("marketing", "problem", "lead", "Our page has 200 followers after a year", "We've posted every week for a year and our social media is dead, no engagement and no new customers.")
F("marketing", "replace_provider", "lead", "Agency results flat", "Our current marketing agency's results have stalled. We're looking to replace them. Any names?")
F("marketing", "vendor_promo", "none", "We grow Instagram accounts", "Marketing agency here. We grow small business Instagram accounts. Call us for pricing.")
F("marketing", "job_seeker", "none", "Social media job hunt", "I'm looking for a social media manager position, remote preferred. Resume attached.")
F("marketing", "chat", "none", "Favourite memes this week", "Share the funniest social media memes you saw this week.")
F("designer", "direct_request", "lead", "Looking for a logo for our app", "We're launching an app next month and I'm looking for a designer to create our logo and brand look.")
F("designer", "recommendation", "lead", "Who did your startup's branding?", "Founders, who did your brand identity? Please recommend a designer you'd work with again.")
F("designer", "buying_question", "lead", "Freelance logo rates?", "What do freelancers usually charge for a logo and brand identity for an early startup?")
F("designer", "problem", "lead", "Our brand feels all over the place", "Our startup's brand is inconsistent everywhere, different logos, colors, fonts, and I don't know where to start fixing it.")
F("designer", "comparison", "lead", "Logo contest sites vs hiring a designer", "Is a logo contest site better than hiring a designer for a new startup brand?")
F("designer", "offering_services", "none", "Open for brand projects", "Brand identity designer with 8 years experience, now taking on new startup clients. Portfolio in profile.")
F("designer", "news", "none", "Brand redesign backlash", "A big retailer's new logo drew criticism online today and the company said it would review the design.")
F("designer", "tutorial", "none", "How I designed my own logo", "I wrote up how I designed my own logo in Figma, sharing the steps.")
F("plumber", "direct_request", "lead", "Leaking tap, need someone in Leeds", "Our kitchen tap has been leaking for days. We need a plumber in Leeds who can come out soon.")
F("plumber", "recommendation", "lead", "Good heating engineer in Leeds?", "Can anyone recommend a good plumber or heating engineer around Leeds for a boiler service?")
F("plumber", "buying_question", "lead", "Cost to unblock a drain", "How much does it cost to get a blocked drain cleared in Leeds? Have a quote for 150 and it feels high.")
F("plumber", "problem", "lead", "No hot water since Monday", "We've had no hot water since Monday, the boiler keeps lock out. Our flat is in Leeds.")
F("plumber", "replace_provider", "lead", "Plumber never turns up", "The plumber we booked in Leeds never turns up. We're looking for someone more reliable for our rental.")
F("plumber", "offtopic_lookalike", "none", "Plumber needed in Manchester", "I'm looking for a plumber in Manchester for a burst pipe, any recommendations?")
F("plumber", "vendor_promo", "none", "Gas safe engineers Leeds", "Leeds gas safe plumbers offering boiler services and repairs. Call us today, free quote.")
F("plumber", "news", "none", "Council pipe works", "The council announced pipe replacement works across the city next month.")
F("skincare", "direct_request", "lead", "Need a cream for cracked hands", "My hands are cracked and sore from the cold. I'm looking for a rich hand cream for very dry skin.")
F("skincare", "recommendation", "lead", "Fragrance free moisturizer suggestions", "Can you suggest a fragrance free moisturizer for sensitive, easily irritated skin?")
F("skincare", "comparison", "lead", "Which is gentler for eczema?", "Which is better for eczema prone dry skin, an ointment or a lotion? Open to buying whichever works.")
F("skincare", "buying_question", "lead", "Are skin barrier creams worth the money?", "Are barrier repair creams actually worth paying for if you have dry sensitive skin?")
F("skincare", "replace_provider", "lead", "My cleanser stopped working", "The cleanser I used for years now irritates my dry skin. I'm switching, what else should I try?")
F("skincare", "vendor_promo", "none", "Launching our balm", "We just launched our new balm for dry skin, 15% off this week with code BALM.")
F("skincare", "offtopic_lookalike", "none", "Dry cleaners nearby", "Looking for a good dry cleaner near the station, recommendations?")
F("skincare", "chat", "none", "Beach photos", "Sharing some photos from my holiday on the beach, great weather.")
F("shopify", "historical", "review", "Need a Shopify theme dev", "We need a developer for our Shopify theme, budget available.", days=500)
F("saas", "historical", "review", "Tool for hiring", "Looking for an applicant tracking tool for our team.", days=360)
F("plumber", "historical", "review", "Plumber in Leeds wanted", "Need a plumber in Leeds for a leaking pipe.", days=420)
F("designer", "ambiguous", "review", "Brand colours", "Thinking about colours for a startup someday, no plans yet.")
F("marketing", "ambiguous", "review", "Marketing is hard", "Marketing a small shop is hard sometimes.")
F("skincare", "ambiguous", "review", "Winter skin", "Winter does things to everyone's skin.")


def items():
    """The dataset as a list of dicts with id and split. Order is the order written above; the split alternates (dev, held, dev, ...) over the
    items of each business, so both splits contain every kind of item."""
    seen, out = {}, []
    for it in _ITEMS:
        n = seen.get(it["biz"], 0)
        if not it.get("fresh"):
            seen[it["biz"]] = n + 1
        kn = sum(1 for o in out if o["biz"] == it["biz"] and o["kind"] == it["kind"]) + 1
        out.append(dict(it, id=f"{it['biz']}-{it['kind']}-{kn}" + ("-f" if it.get("fresh") else ""), split="fresh" if it.get("fresh") else ("dev" if n % 2 == 0 else "held")))
    return out
