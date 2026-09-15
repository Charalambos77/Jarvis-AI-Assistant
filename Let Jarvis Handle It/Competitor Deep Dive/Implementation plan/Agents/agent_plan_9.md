# Agent Spawn Plan - Plan ID: 9
**Task Summary:** Perform deep research on top 20 competitors from 5 English-speaking countries (medium economy, emerging AI adoption), covering marketing, ads, selling points, products/services, pricing, SEO keywords, customer experience, selling process, and full website breakdown for an agency offering AI solutions and custom software, then upload findings to Google Docs.
**Task Type:** research

## Research Cycles
### Cycle 1: Country Selection & Market Definition
- **Goal:** Identify 5 English-speaking countries with medium economies and emerging AI adoption trends.
- **Lead Specialist:** Economic & AI Trend Analyst (ID: economic_ai_trend_analyst_cycle1_lead)
  - Brief: Synthesize findings from the 'Geopolitical Data Researcher' and 'AI Adoption Indicator Specialist' for a comprehensive list of potential English-speaking countries. Select the five best countries that meet all criteria: English-speaking, medium economy (GDP between $200B and $1T USD nominal, 2023-2024 estimates), and showing emerging AI adoption within the last 1-2 years. For each selected country, provide clear justification. For any country considered but not selected, explicitly state the reason for exclusion, ensuring to address all countries provided by advisors, including Pakistan and Hong Kong if they were presented.
- **Advisory Agents:**
  - Geopolitical Data Researcher (ID: geopolitical_data_researcher_cycle1_adv_1): Research potential English-speaking countries globally that could fit the 'medium economy' criteria (GDP between $200B and $1T USD nominal, based on 2023-2024 estimates). For each identified country, provide its GDP, a brief source, and indicate if it falls within the specified range. Your research should be broad to cover all plausible candidates, including countries like Pakistan and Hong Kong.
  - AI Adoption Indicator Specialist (ID: ai_adoption_indicator_specialist_cycle1_adv_2): For a comprehensive list of potential English-speaking countries, research indicators of emerging AI adoption within the last 1-2 years. Look for evidence of national AI strategies, growth in local AI communities/meetups, AI-related websites, public data on AI application usage, and recent news highlighting initial AI integration across business sectors. Provide a summary of AI adoption status and supporting evidence for each country, including countries like Pakistan and Hong Kong.

### Cycle 2: Competitor Identification
- **Goal:** Identify 20 'top' competitor agencies per chosen country (100 total) that offer AI solutions and custom software for B2B clients.
- **Lead Specialist:** Market Research Specialist (ID: market_research_specialist_cycle2_lead)
  - Brief: For each of the 5 countries selected in Cycle 1, identify 20 'top' competitor agencies. These agencies must primarily offer AI solutions and/or custom software development for B2B clients. 'Top' is defined by high online visibility (first page search results for relevant terms like 'B2B AI solutions agency', 'custom AI software development B2B'), implied conversion (professional online presence, client testimonials), and recognition as a niche leader. Provide names, websites, and a brief justification for each selection.
- **Advisory Agents:**
  - SEO Visibility Analyst (ID: seo_visibility_analyst_cycle2_adv_1): For each of the 5 countries chosen in Cycle 1, conduct targeted Google searches using terms like 'AI solutions agency [country name]', 'custom AI software B2B [country name]', 'AI development services [country name]'. Identify agencies that appear prominently in organic search results (first page) as an indicator of high visibility. Prioritize agencies that clearly state they serve B2B clients with AI solutions and/or custom software. Collect the names and URLs of potential top competitors.
  - B2B Niche Expert (ID: b2b_niche_expert_cycle2_adv_2): Review the websites of potential competitors identified by other agents in this cycle. Evaluate their service offerings to confirm they primarily focus on AI solutions and/or custom software for B2B clients. Look for clear messaging, case studies, or client lists that indicate B2B focus and their positioning as a leader in this niche. Identify any freelancers or companies primarily offering general marketing without a strong AI/custom software core and flag them for exclusion. Identify agencies that demonstrate conversion through testimonials or case studies.

### Cycle 3: Competitor Deep Dive - Marketing & Sales
- **Goal:** Research marketing strategies, ads, selling points, products/services, pricing models, customer experience, and selling process for each identified competitor (100 total).
- **Lead Specialist:** Marketing Strategy Analyst (ID: marketing_strategy_analyst_cycle3_lead)
  - Brief: For each competitor identified in Cycle 2, analyze their overall marketing strategies. Identify their primary target audience, key messaging, and distribution channels. Determine their main selling points and differentiate between their core AI solutions and custom software offerings. Document any publicly available pricing models. Synthesize findings into a concise overview for each competitor.
- **Advisory Agents:**
  - Ad Campaign Researcher (ID: ad_campaign_researcher_cycle3_adv_1): For each competitor identified in Cycle 2, research their advertising efforts. Identify platforms used (e.g., Google Ads, LinkedIn Ads, social media ads if visible through search) and common ad messaging. Capture examples of ad copy or creatives if publicly accessible. Note if they appear to run extensive ad campaigns or minimal advertising.
  - Sales Process Analyst (ID: sales_process_analyst_cycle3_adv_2): For each competitor identified in Cycle 2, investigate their customer experience and selling process. This involves analyzing their website for 'Contact Us' forms, booking tools, whitepapers, webinars, or any indicators of their sales funnel. Look for testimonials, case studies, or public reviews that offer insights into client interaction and satisfaction. Describe the typical steps a potential client would take from initial inquiry to becoming a customer, based on public information.

### Cycle 4: Competitor Deep Dive - Website & SEO
- **Goal:** Conduct a full website breakdown and identify SEO keywords for each identified competitor (100 total).
- **Lead Specialist:** Web Content Analyst (ID: web_content_analyst_cycle4_lead)
  - Brief: For each competitor identified in Cycle 2, conduct a comprehensive website breakdown. Focus on 'Big Importance' elements: main service descriptions (for AI/custom software), clear CTAs, value propositions, detailed testimonials/case studies, public pricing, and clear navigation to core solutions. Analyze 'Medium Importance' elements: high-quality imagery, 'About Us'/'Team' pages, relevant blog content. Note 'Small Importance' elements: specific font choices, minor decorative design, general layouts unless hindering UX. Document copywriting styles, types of pages/sub-pages, overall site style, and heading structures.
- **Advisory Agents:**
  - SEO Keyword Researcher (ID: seo_keyword_researcher_cycle4_adv_1): For each competitor identified in Cycle 2, identify their primary SEO keywords. Use free online tools and site-specific analysis (e.g., examining page titles, meta descriptions, headings, and core content) to determine the key terms they are targeting to attract organic traffic for AI solutions and custom software services. Prioritize high-volume or highly relevant keywords.
  - UI/UX Pattern Analyst (ID: ui_ux_pattern_analyst_cycle4_adv_2): For each competitor identified in Cycle 2, analyze the UI/UX patterns and design choices on their website. Evaluate the overall user experience, clarity of information architecture, and effectiveness of visual design in conveying their message. Identify common design elements, navigation patterns, and visual themes that appear to be effective or noteworthy within the B2B AI/custom software niche. Pay attention to how different sections (e.g., testimonials, service pages, contact forms) are designed and laid out.

## Execution Agents
### Google Docs Uploader (ID: google_docs_uploader_exec_1)
- **Brief:** Receive the compiled competitor research data from all research cycles and upload it to Google Docs. Organize each competitor's data into individual documents as per the specified structure (Big, Medium, Small importance). Ensure all content is accurately transferred and formatted for readability.
- **Required Keys:** google_doc_urls, organization_structure_notes
- **Min Word Count:** 0

## Full JSON Payload
```json
{
  "task_summary": "Perform deep research on top 20 competitors from 5 English-speaking countries (medium economy, emerging AI adoption), covering marketing, ads, selling points, products/services, pricing, SEO keywords, customer experience, selling process, and full website breakdown for an agency offering AI solutions and custom software, then upload findings to Google Docs.",
  "task_type": "research",
  "cycles": [
    {
      "cycle_id": 1,
      "depends_on": [],
      "domain": "Country Selection & Market Definition",
      "goal": "Identify 5 English-speaking countries with medium economies and emerging AI adoption trends.",
      "lead_specialist": {
        "agent_id": "economic_ai_trend_analyst_cycle1_lead",
        "role": "Economic & AI Trend Analyst",
        "brief": "Synthesize findings from the 'Geopolitical Data Researcher' and 'AI Adoption Indicator Specialist' for a comprehensive list of potential English-speaking countries. Select the five best countries that meet all criteria: English-speaking, medium economy (GDP between $200B and $1T USD nominal, 2023-2024 estimates), and showing emerging AI adoption within the last 1-2 years. For each selected country, provide clear justification. For any country considered but not selected, explicitly state the reason for exclusion, ensuring to address all countries provided by advisors, including Pakistan and Hong Kong if they were presented.",
        "tools_needed": [
          "google_search"
        ],
        "memory_query": "countries with emerging AI adoption, national AI strategies report, GDP by country list"
      },
      "advisory_agents": [
        {
          "agent_id": "geopolitical_data_researcher_cycle1_adv_1",
          "role": "Geopolitical Data Researcher",
          "brief": "Research potential English-speaking countries globally that could fit the 'medium economy' criteria (GDP between $200B and $1T USD nominal, based on 2023-2024 estimates). For each identified country, provide its GDP, a brief source, and indicate if it falls within the specified range. Your research should be broad to cover all plausible candidates, including countries like Pakistan and Hong Kong.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "countries by GDP 2023, official languages of countries"
        },
        {
          "agent_id": "ai_adoption_indicator_specialist_cycle1_adv_2",
          "role": "AI Adoption Indicator Specialist",
          "brief": "For a comprehensive list of potential English-speaking countries, research indicators of emerging AI adoption within the last 1-2 years. Look for evidence of national AI strategies, growth in local AI communities/meetups, AI-related websites, public data on AI application usage, and recent news highlighting initial AI integration across business sectors. Provide a summary of AI adoption status and supporting evidence for each country, including countries like Pakistan and Hong Kong.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "emerging AI markets, AI strategy by country, AI startup growth reports"
        }
      ]
    },
    {
      "cycle_id": 2,
      "depends_on": [
        1
      ],
      "domain": "Competitor Identification",
      "goal": "Identify 20 'top' competitor agencies per chosen country (100 total) that offer AI solutions and custom software for B2B clients.",
      "lead_specialist": {
        "agent_id": "market_research_specialist_cycle2_lead",
        "role": "Market Research Specialist",
        "brief": "For each of the 5 countries selected in Cycle 1, identify 20 'top' competitor agencies. These agencies must primarily offer AI solutions and/or custom software development for B2B clients. 'Top' is defined by high online visibility (first page search results for relevant terms like 'B2B AI solutions agency', 'custom AI software development B2B'), implied conversion (professional online presence, client testimonials), and recognition as a niche leader. Provide names, websites, and a brief justification for each selection.",
        "tools_needed": [
          "google_search"
        ],
        "memory_query": "identifying top B2B agencies, competitor selection criteria"
      },
      "advisory_agents": [
        {
          "agent_id": "seo_visibility_analyst_cycle2_adv_1",
          "role": "SEO Visibility Analyst",
          "brief": "For each of the 5 countries chosen in Cycle 1, conduct targeted Google searches using terms like 'AI solutions agency [country name]', 'custom AI software B2B [country name]', 'AI development services [country name]'. Identify agencies that appear prominently in organic search results (first page) as an indicator of high visibility. Prioritize agencies that clearly state they serve B2B clients with AI solutions and/or custom software. Collect the names and URLs of potential top competitors.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "local SEO search strategies, B2B AI agency search terms"
        },
        {
          "agent_id": "b2b_niche_expert_cycle2_adv_2",
          "role": "B2B Niche Expert",
          "brief": "Review the websites of potential competitors identified by other agents in this cycle. Evaluate their service offerings to confirm they primarily focus on AI solutions and/or custom software for B2B clients. Look for clear messaging, case studies, or client lists that indicate B2B focus and their positioning as a leader in this niche. Identify any freelancers or companies primarily offering general marketing without a strong AI/custom software core and flag them for exclusion. Identify agencies that demonstrate conversion through testimonials or case studies.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "B2B AI services differentiation, identifying agency niche"
        }
      ]
    },
    {
      "cycle_id": 3,
      "depends_on": [
        2
      ],
      "domain": "Competitor Deep Dive - Marketing & Sales",
      "goal": "Research marketing strategies, ads, selling points, products/services, pricing models, customer experience, and selling process for each identified competitor (100 total).",
      "lead_specialist": {
        "agent_id": "marketing_strategy_analyst_cycle3_lead",
        "role": "Marketing Strategy Analyst",
        "brief": "For each competitor identified in Cycle 2, analyze their overall marketing strategies. Identify their primary target audience, key messaging, and distribution channels. Determine their main selling points and differentiate between their core AI solutions and custom software offerings. Document any publicly available pricing models. Synthesize findings into a concise overview for each competitor.",
        "tools_needed": [
          "google_search"
        ],
        "memory_query": "competitor marketing strategy analysis, B2B AI service differentiation"
      },
      "advisory_agents": [
        {
          "agent_id": "ad_campaign_researcher_cycle3_adv_1",
          "role": "Ad Campaign Researcher",
          "brief": "For each competitor identified in Cycle 2, research their advertising efforts. Identify platforms used (e.g., Google Ads, LinkedIn Ads, social media ads if visible through search) and common ad messaging. Capture examples of ad copy or creatives if publicly accessible. Note if they appear to run extensive ad campaigns or minimal advertising.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "competitor ad analysis tools free, identifying B2B ad campaigns"
        },
        {
          "agent_id": "sales_process_analyst_cycle3_adv_2",
          "role": "Sales Process Analyst",
          "brief": "For each competitor identified in Cycle 2, investigate their customer experience and selling process. This involves analyzing their website for 'Contact Us' forms, booking tools, whitepapers, webinars, or any indicators of their sales funnel. Look for testimonials, case studies, or public reviews that offer insights into client interaction and satisfaction. Describe the typical steps a potential client would take from initial inquiry to becoming a customer, based on public information.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "B2B sales funnel analysis, customer experience indicators online"
        }
      ]
    },
    {
      "cycle_id": 4,
      "depends_on": [
        2
      ],
      "domain": "Competitor Deep Dive - Website & SEO",
      "goal": "Conduct a full website breakdown and identify SEO keywords for each identified competitor (100 total).",
      "lead_specialist": {
        "agent_id": "web_content_analyst_cycle4_lead",
        "role": "Web Content Analyst",
        "brief": "For each competitor identified in Cycle 2, conduct a comprehensive website breakdown. Focus on 'Big Importance' elements: main service descriptions (for AI/custom software), clear CTAs, value propositions, detailed testimonials/case studies, public pricing, and clear navigation to core solutions. Analyze 'Medium Importance' elements: high-quality imagery, 'About Us'/'Team' pages, relevant blog content. Note 'Small Importance' elements: specific font choices, minor decorative design, general layouts unless hindering UX. Document copywriting styles, types of pages/sub-pages, overall site style, and heading structures.",
        "tools_needed": [
          "google_search"
        ],
        "memory_query": "website analysis framework B2B, identifying key website elements"
      },
      "advisory_agents": [
        {
          "agent_id": "seo_keyword_researcher_cycle4_adv_1",
          "role": "SEO Keyword Researcher",
          "brief": "For each competitor identified in Cycle 2, identify their primary SEO keywords. Use free online tools and site-specific analysis (e.g., examining page titles, meta descriptions, headings, and core content) to determine the key terms they are targeting to attract organic traffic for AI solutions and custom software services. Prioritize high-volume or highly relevant keywords.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "free SEO keyword research tools, on-page SEO analysis"
        },
        {
          "agent_id": "ui_ux_pattern_analyst_cycle4_adv_2",
          "role": "UI/UX Pattern Analyst",
          "brief": "For each competitor identified in Cycle 2, analyze the UI/UX patterns and design choices on their website. Evaluate the overall user experience, clarity of information architecture, and effectiveness of visual design in conveying their message. Identify common design elements, navigation patterns, and visual themes that appear to be effective or noteworthy within the B2B AI/custom software niche. Pay attention to how different sections (e.g., testimonials, service pages, contact forms) are designed and laid out.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "B2B website UI/UX best practices, effective website design patterns"
        }
      ]
    }
  ],
  "recommended_tools": [
    {
      "service": "google_search",
      "purpose": "General web research and data gathering for all cycles and agents.",
      "doc_url": "https://developers.google.com/custom-search/v1/overview",
      "recommended_by": [
        "economic_ai_trend_analyst_cycle1_lead",
        "geopolitical_data_researcher_cycle1_adv_1",
        "ai_adoption_indicator_specialist_cycle1_adv_2",
        "market_research_specialist_cycle2_lead",
        "seo_visibility_analyst_cycle2_adv_1",
        "b2b_niche_expert_cycle2_adv_2",
        "marketing_strategy_analyst_cycle3_lead",
        "ad_campaign_researcher_cycle3_adv_1",
        "sales_process_analyst_cycle3_adv_2",
        "web_content_analyst_cycle4_lead",
        "seo_keyword_researcher_cycle4_adv_1",
        "ui_ux_pattern_analyst_cycle4_adv_2"
      ],
      "pros": [
        "Extensive public data access",
        "Versatile for diverse queries"
      ],
      "cons": [
        "Requires careful query formulation",
        "Information quality varies"
      ],
      "alternatives": [
        "bing_search"
      ],
      "connection_methods": [
        {
          "method_id": "browser_access",
          "label": "Direct Browser Access (manual)",
          "fields": []
        }
      ]
    },
    {
      "service": "google_docs_api",
      "purpose": "Upload and manage research documents in Google Docs.",
      "doc_url": "https://developers.google.com/docs/api/overview",
      "recommended_by": [
        "google_docs_uploader_exec_1"
      ],
      "pros": [
        "Automated document creation and organization",
        "Integration with Google Workspace"
      ],
      "cons": [
        "Requires OAuth authentication setup",
        "API rate limits"
      ],
      "alternatives": [
        "manual_google_drive_upload"
      ],
      "connection_methods": [
        {
          "method_id": "oauth",
          "label": "OAuth 2.0 Client",
          "fields": [
            {
              "name": "client_id",
              "label": "Client ID",
              "type": "text"
            },
            {
              "name": "client_secret",
              "label": "Client Secret",
              "type": "password"
            },
            {
              "name": "refresh_token",
              "label": "Refresh Token",
              "type": "password"
            }
          ]
        }
      ]
    }
  ],
  "execution_agents": [
    {
      "agent_id": "google_docs_uploader_exec_1",
      "role": "Google Docs Uploader",
      "brief": "Receive the compiled competitor research data from all research cycles and upload it to Google Docs. Organize each competitor's data into individual documents as per the specified structure (Big, Medium, Small importance). Ensure all content is accurately transferred and formatted for readability.",
      "tools_needed": [
        "google_docs_api"
      ],
      "output_spec": {
        "required_keys": [
          "google_doc_urls",
          "organization_structure_notes"
        ],
        "min_documents_uploaded": 100
      }
    }
  ]
}
```
