# Agent Spawn Plan - Plan ID: bd80503d
**Task Summary:** Create a comprehensive YouTube video guide on 'Sustainable Urban Farming,' covering key techniques, benefits, and practical steps for an eco-conscious audience.
**Task Type:** video

## Research Cycles
### Cycle 1: Audience & Niche Analysis
- **Goal:** Define the target audience persona, understand their needs and interests, and identify a unique angle for the 'Sustainable Urban Farming' content.
- **Lead Specialist:** Audience Strategist (ID: cycle1_lead)
  - Brief: Research and define the ideal viewer persona for urban farming content, including demographics, interests, and existing knowledge gaps.
- **Advisory Agents:**
  - Niche Content Identifier (ID: cycle1_adv_1): Analyze existing YouTube content on urban farming to identify underserved topics, common questions, and potential unique selling propositions for our video.

### Cycle 2: Content Strategy & Subject Matter Expertise
- **Goal:** Gather in-depth, accurate information on sustainable urban farming techniques, benefits, and challenges, and outline a structured, engaging video narrative.
- **Lead Specialist:** Subject Matter Expert (Urban Farming) (ID: cycle2_lead)
  - Brief: Research the most impactful and practical sustainable urban farming techniques, including composting, vertical gardening, hydroponics, and permaculture principles. Verify accuracy and currency of information.
- **Advisory Agents:**
  - Content Structurer (ID: cycle2_adv_1): Develop a logical flow and outline for the video, ensuring comprehensive coverage of key topics identified by the SME while maintaining viewer engagement. Suggest visual aid opportunities.
  - Trend Analyst (ID: cycle2_adv_2): Identify current trends and innovations in urban farming that could make the video more relevant and appealing to a modern audience.

### Cycle 3: Platform Optimization & Virality
- **Goal:** Develop a strategy for YouTube SEO, viewer engagement, and discoverability to maximize the video's reach and impact.
- **Lead Specialist:** YouTube SEO Specialist (ID: cycle3_lead)
  - Brief: Conduct keyword research to identify high-volume, relevant terms for the video title, description, and tags. Analyze competitor video SEO strategies.
- **Advisory Agents:**
  - Engagement Strategist (ID: cycle3_adv_1): Recommend compelling hooks, calls-to-action, and viewer interaction elements (e.g., polls, questions) to increase watch time and subscriber conversion.
  - Thumbnail & Title Advisor (ID: cycle3_adv_2): Provide guidance on creating clickable thumbnails and optimized titles that attract target viewers and clearly communicate video value.

## Execution Agents
### Script Writer (ID: agent_exec_1)
- **Brief:** Develop a detailed, engaging, and accurate video script based on the synthesized research blueprint, incorporating SEO keywords and engagement strategies.
- **Required Keys:** title_options, hook, introduction, main_segments, conclusion, call_to_action, visual_cues, on_screen_text_suggestions
- **Min Word Count:** 1500

### Video Editor (ID: agent_exec_2)
- **Brief:** Produce a high-quality video edit from provided footage, graphics, voiceovers, and music, adhering to the script and visual guidelines.
- **Required Keys:** final_video_file_mp4, lower_thirds_assets, intro_outro_sequence
- **Min Word Count:** 0

### Thumbnail Designer (ID: agent_exec_3)
- **Brief:** Create a compelling and clickable YouTube thumbnail that accurately represents the video content and attracts the target audience, based on design guidelines.
- **Required Keys:** thumbnail_image_jpg_png
- **Min Word Count:** 0

## Full JSON Payload
```json
{
  "task_summary": "Create a comprehensive YouTube video guide on 'Sustainable Urban Farming,' covering key techniques, benefits, and practical steps for an eco-conscious audience.",
  "task_type": "video",
  "cycles": [
    {
      "cycle_id": 1,
      "domain": "Audience & Niche Analysis",
      "goal": "Define the target audience persona, understand their needs and interests, and identify a unique angle for the 'Sustainable Urban Farming' content.",
      "lead_specialist": {
        "agent_id": "cycle1_lead",
        "role": "Audience Strategist",
        "brief": "Research and define the ideal viewer persona for urban farming content, including demographics, interests, and existing knowledge gaps.",
        "tools_needed": [
          "google_search",
          "social_media_analytics",
          "audience_survey_tool"
        ],
        "memory_query": "YouTube audience segmentation for sustainability topics"
      },
      "advisory_agents": [
        {
          "agent_id": "cycle1_adv_1",
          "role": "Niche Content Identifier",
          "brief": "Analyze existing YouTube content on urban farming to identify underserved topics, common questions, and potential unique selling propositions for our video.",
          "tools_needed": [
            "google_search",
            "youtube_search"
          ],
          "memory_query": "content gap analysis for educational YouTube videos"
        }
      ]
    },
    {
      "cycle_id": 2,
      "domain": "Content Strategy & Subject Matter Expertise",
      "goal": "Gather in-depth, accurate information on sustainable urban farming techniques, benefits, and challenges, and outline a structured, engaging video narrative.",
      "lead_specialist": {
        "agent_id": "cycle2_lead",
        "role": "Subject Matter Expert (Urban Farming)",
        "brief": "Research the most impactful and practical sustainable urban farming techniques, including composting, vertical gardening, hydroponics, and permaculture principles. Verify accuracy and currency of information.",
        "tools_needed": [
          "google_search",
          "academic_database_access"
        ],
        "memory_query": "best practices for sustainable urban farming methods"
      },
      "advisory_agents": [
        {
          "agent_id": "cycle2_adv_1",
          "role": "Content Structurer",
          "brief": "Develop a logical flow and outline for the video, ensuring comprehensive coverage of key topics identified by the SME while maintaining viewer engagement. Suggest visual aid opportunities.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "educational video script outlining best practices"
        },
        {
          "agent_id": "cycle2_adv_2",
          "role": "Trend Analyst",
          "brief": "Identify current trends and innovations in urban farming that could make the video more relevant and appealing to a modern audience.",
          "tools_needed": [
            "google_search",
            "news_aggregator"
          ],
          "memory_query": "emerging trends in sustainable agriculture"
        }
      ]
    },
    {
      "cycle_id": 3,
      "domain": "Platform Optimization & Virality",
      "goal": "Develop a strategy for YouTube SEO, viewer engagement, and discoverability to maximize the video's reach and impact.",
      "lead_specialist": {
        "agent_id": "cycle3_lead",
        "role": "YouTube SEO Specialist",
        "brief": "Conduct keyword research to identify high-volume, relevant terms for the video title, description, and tags. Analyze competitor video SEO strategies.",
        "tools_needed": [
          "google_search",
          "youtube_search",
          "keyword_research_tool"
        ],
        "memory_query": "YouTube video SEO best practices for educational content"
      },
      "advisory_agents": [
        {
          "agent_id": "cycle3_adv_1",
          "role": "Engagement Strategist",
          "brief": "Recommend compelling hooks, calls-to-action, and viewer interaction elements (e.g., polls, questions) to increase watch time and subscriber conversion.",
          "tools_needed": [
            "google_search"
          ],
          "memory_query": "YouTube engagement strategies for non-entertainment channels"
        },
        {
          "agent_id": "cycle3_adv_2",
          "role": "Thumbnail & Title Advisor",
          "brief": "Provide guidance on creating clickable thumbnails and optimized titles that attract target viewers and clearly communicate video value.",
          "tools_needed": [
            "google_search",
            "image_analysis_tool"
          ],
          "memory_query": "high-performing YouTube thumbnail and title patterns"
        }
      ]
    }
  ],
  "recommended_tools": [
    {
      "service": "youtube_api",
      "purpose": "Upload final video, manage metadata (title, description, tags), schedule release, and track performance analytics.",
      "recommended_by": [
        "cycle3_lead",
        "cycle3_adv_1"
      ],
      "pros": [
        "Direct integration with YouTube platform",
        "Comprehensive metadata control",
        "Access to analytics data"
      ],
      "cons": [
        "Requires API key and OAuth setup",
        "Rate limits can apply"
      ],
      "alternatives": [
        "manual_youtube_upload"
      ]
    },
    {
      "service": "google_search",
      "purpose": "General research, competitor analysis, trend identification, and validation of information across all research cycles.",
      "recommended_by": [
        "cycle1_lead",
        "cycle2_lead",
        "cycle3_lead"
      ],
      "pros": [
        "Vast information index",
        "Quick access to diverse sources",
        "Essential for initial discovery"
      ],
      "cons": [
        "Requires critical evaluation of sources",
        "Information overload risk"
      ],
      "alternatives": [
        "bing_search",
        "duckduckgo_search"
      ]
    },
    {
      "service": "keyword_research_tool",
      "purpose": "Identify high-performing keywords for YouTube video titles, descriptions, and tags to improve search ranking and discoverability.",
      "recommended_by": [
        "cycle3_lead"
      ],
      "pros": [
        "Quantifies search volume and competition",
        "Suggests related keywords",
        "Essential for SEO optimization"
      ],
      "cons": [
        "Subscription costs for advanced features",
        "Data can sometimes be outdated"
      ],
      "alternatives": [
        "manual_youtube_search_suggestions"
      ]
    }
  ],
  "execution_agents": [
    {
      "agent_id": "agent_exec_1",
      "role": "Script Writer",
      "brief": "Develop a detailed, engaging, and accurate video script based on the synthesized research blueprint, incorporating SEO keywords and engagement strategies.",
      "tools_needed": [
        "google_search"
      ],
      "output_spec": {
        "required_keys": [
          "title_options",
          "hook",
          "introduction",
          "main_segments",
          "conclusion",
          "call_to_action",
          "visual_cues",
          "on_screen_text_suggestions"
        ],
        "min_word_count": 1500,
        "format": "Markdown"
      }
    },
    {
      "agent_id": "agent_exec_2",
      "role": "Video Editor",
      "brief": "Produce a high-quality video edit from provided footage, graphics, voiceovers, and music, adhering to the script and visual guidelines.",
      "tools_needed": [
        "video_editing_software_suite"
      ],
      "output_spec": {
        "required_keys": [
          "final_video_file_mp4",
          "lower_thirds_assets",
          "intro_outro_sequence"
        ],
        "min_duration_minutes": 10,
        "max_duration_minutes": 20,
        "resolution": "1080p"
      }
    },
    {
      "agent_id": "agent_exec_3",
      "role": "Thumbnail Designer",
      "brief": "Create a compelling and clickable YouTube thumbnail that accurately represents the video content and attracts the target audience, based on design guidelines.",
      "tools_needed": [
        "image_editing_software"
      ],
      "output_spec": {
        "required_keys": [
          "thumbnail_image_jpg_png"
        ],
        "resolution": "1280x720",
        "file_size_mb": 2
      }
    }
  ]
}
```
