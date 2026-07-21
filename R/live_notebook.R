library(shiny)
library(bslib)
library(tidyverse)
library(RSQLite)
library(DT)
library(shinyjs)
library(uuid) 

# --- CONFIGURATION ---
if (file.exists("lab_notebook.db")) {
  DB_PATH <- "lab_notebook.db"
} else {
  DB_PATH <- "C:\\Users\\chase\\Documents\\Everything_Python\\lab_notebook.db"
}

# --- ASSET MANAGEMENT ---
UPLOAD_DIR <- "www/uploads"
if (!dir.exists(UPLOAD_DIR)) dir.create(UPLOAD_DIR, recursive = TRUE)

# --- DATABASE MIGRATION (AUTO-RUN) ---
con <- dbConnect(SQLite(), DB_PATH)
tryCatch({
  dbExecute(con, "CREATE TABLE IF NOT EXISTS entry_history (history_id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER, old_transcript TEXT, archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(parent_id) REFERENCES lab_notes(id))")
  dbExecute(con, "CREATE TABLE IF NOT EXISTS entry_attachments (attach_id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER, filename TEXT, uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(parent_id) REFERENCES lab_notes(id))")
  tryCatch({ dbGetQuery(con, "SELECT deleted_at FROM lab_notes LIMIT 1") }, error = function(e) { dbExecute(con, "ALTER TABLE lab_notes ADD COLUMN deleted_at TIMESTAMP DEFAULT NULL") })
}, finally = { dbDisconnect(con) })

# --- UI ---
ui <- tagList(
  includeCSS("styles.css"),
  useShinyjs(),
  
  # --- CUSTOM JS ---
  tags$script(HTML("
    $(document).on('keyup', function(e) {
      if(e.which == 13 && !e.shiftKey) { 
        var tagInput = $('#quick_note_input').next('.selectize-control').find('.selectize-input');
        if (tagInput.hasClass('focus')) { $('#save_quick_note').click(); }
      }
      if((e.ctrlKey || e.metaKey) && e.which == 13) {
         if ($('#entry_text_input').is(':focus')) { $('#save_entry_inline').click(); }
      }
    });
    $(document).on('keyup', function(e) { if(e.which == 27) { $('#floating_editor').hide(); $('#floating_entry_editor').hide(); } });
    $(document).on('scroll', '.dataTables_scrollBody', function() { $('#floating_editor').hide(); $('#floating_entry_editor').hide(); });

    $(document).on('click', '.btn-edit-note, .btn-append-note, .ghost-pill, .tag-pill', function(e) {
      e.stopPropagation(); $('#floating_entry_editor').hide();
      var target = $(this).closest('.note-cell-wrapper');
      if (target.length === 0) target = $(this).parent(); if (target.length === 0) target = $(this);
      var rect = target[0].getBoundingClientRect();
      var editor = $('#floating_editor');
      editor.css({ top: (rect.top - 8) + 'px', left: (rect.left - 5) + 'px', width: (rect.width + 15) + 'px', display: 'flex' });
    });
    
    $(document).on('click', '.entry-text-clickable', function(e) {
      e.stopPropagation(); $('#floating_editor').hide();
      var target = $(this);
      var ids = target.attr('data-ids'); 
      var content = target.attr('data-content'); 
      Shiny.setInputValue('trigger_inline_edit', {ids: ids, content: content}, {priority: 'event'});
      var rect = target[0].getBoundingClientRect();
      var editor = $('#floating_entry_editor');
      editor.css({ top: (rect.top - 10) + 'px', left: (rect.left - 10) + 'px', width: (rect.width + 40) + 'px', height: 'auto', minHeight: (rect.height + 20) + 'px', display: 'flex' });
      setTimeout(function() { $('#entry_text_input').focus(); }, 50);
    });
    
    $(document).on('click', '.filter-pill', function(e) {
       var cat = $(this).data('cat');
       Shiny.setInputValue('filter_category', cat, {priority: 'event'});
    });
    
    $(document).on('click', '#cancel_entry_inline', function(e) {
       $('#floating_entry_editor').hide();
    });
    
    $(document).on('click', '#floating_entry_editor, .btn-entry-tool, .attachment-thumb', function(e) { e.stopPropagation(); });
  ")),
  
  div(
    id = "app_container",
    style = "display: flex; flex-direction: column; min-height: 100vh; background-color: #202123; padding: 15px; box-sizing: border-box; gap: 15px;",
    
    # --- CSS OVERRIDES ---
    tags$style(HTML("
      /* VARIABLES */
      :root {
        --dt-row-selected: 32, 33, 35 !important;      
        --dt-row-selected-text: 236, 236, 241 !important; 
        --dt-row-selected-link: 0, 188, 212 !important;   
      }

      /* LAYOUT */
      .hud-bar { 
        background: rgba(30, 30, 30, 0.95); 
        backdrop-filter: blur(15px); 
        border: 1px solid rgba(255, 255, 255, 0.1); 
        border-radius: 20px; 
        padding: 10px 25px; 
        box-shadow: 0 10px 30px rgba(0,0,0,0.5); 
        position: sticky; top: 15px; z-index: 900; 
        min-height: 90px; 
        height: auto;
        display: flex; 
        align-items: center; 
        justify-content: space-between; 
      }
      .stat-pill { background: #1E1E1E; border: 1px solid #333; color: #ECECF1; padding: 5px 15px; border-radius: 20px; font-size: 0.85em; font-weight: 600; display: flex; align-items: center; gap: 8px; box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); white-space: nowrap; }
      .notebook-card { background: rgba(30, 30, 30, 0.4); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); display: block; position: relative; height: auto; overflow: visible; }
      
      /* FILTER PILLS (MOSAIC STYLE) */
      #category_breakdown { -ms-overflow-style: none; scrollbar-width: none; }
      #category_breakdown::-webkit-scrollbar { display: none; }
      
      .filter-pill { 
        cursor: pointer; transition: all 0.1s ease; opacity: 0.8; 
        white-space: nowrap; font-size: 0.7em !important; padding: 4px 10px !important;
        border-radius: 6px !important; font-weight: 700 !important; margin: 0 !important; 
      }
      .filter-pill:hover { opacity: 1; filter: brightness(1.2); z-index: 2; }
      .filter-pill.active { opacity: 1; box-shadow: 0 0 15px rgba(0,0,0,0.5); border-color: #fff !important; z-index: 2; transform: scale(1.05); }
      
      /* EDITORS */
      #floating_editor { position: fixed; z-index: 9999; background-color: #1a1a1a; border: 1px solid var(--accent-primary); border-radius: 12px; padding: 5px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); display: none; align-items: center; gap: 8px; }
      #floating_editor .selectize-input { background-color: #111 !important; border: none !important; color: #fff !important; min-height: 32px !important; }
      #floating_editor .selectize-input input { color: #fff !important; }
      #floating_editor .selectize-input .item { background-color: var(--accent-primary) !important; color: #111 !important; border-radius: 4px; padding: 2px 6px; font-weight: 600; text-shadow: none; }
      #save_quick_note { width: 32px !important; height: 32px !important; border-radius: 50% !important; padding: 0 !important; background-color: var(--accent-primary) !important; color: #111 !important; border: none !important; display: flex; align-items: center; justify-content: center; }

      #floating_entry_editor { position: fixed; z-index: 10000; background-color: #1a1a1a; border: 2px solid var(--accent-primary); border-radius: 8px; padding: 10px; box-shadow: 0 15px 40px rgba(0,0,0,0.8); display: none; flex-direction: column; gap: 10px; }
      #entry_text_input { background-color: #111; border: 1px solid #333; color: #eee; border-radius: 6px; padding: 10px; font-family: 'Inter', sans-serif; font-size: 1.05em; line-height: 1.5; resize: none; width: 100%; height: 150px; }
      #entry_text_input:focus { outline: none; border-color: #555; }
      .editor-footer { display: flex; justify-content: space-between; align-items: center; }
      .editor-hint { color: #666; font-size: 0.8em; font-style: italic; }

      /* TABLE */
      table.dataTable tbody tr { transition: none !important; transform: none !important; box-shadow: none !important; }
      table.dataTable tbody tr:hover { background-color: #2A2B32 !important; z-index: auto !important; }
      table.dataTable.display tbody > tr.selected, table.dataTable.display tbody > tr.selected:hover, table.dataTable tbody > tr.selected > td { background-color: rgba(0, 188, 212, 0.08) !important; color: inherit !important; box-shadow: inset 3px 0 0 var(--accent-primary) !important; background-image: none !important; }

      /* SEARCH BAR STYLING */
      .dataTables_filter { margin-bottom: 10px; }
      .dataTables_filter input {
        background-color: #111;
        border: 1px solid #444;
        color: #eee;
        border-radius: 20px;
        padding: 5px 15px;
        outline: none;
        margin-left: 10px;
      }
      .dataTables_filter input:focus { border-color: var(--accent-primary); }
      .dataTables_filter label { color: #888; font-weight: normal; font-size: 0.9em; }

      /* PILLS & TEXT */
      .tag-pill { display: inline-block; background: rgba(0, 188, 212, 0.15); border: 1px solid rgba(0, 188, 212, 0.3); color: #00e5ff; padding: 2px 8px; border-radius: 6px; font-size: 0.85em; margin-right: 5px; margin-bottom: 2px; font-weight: 500; cursor: pointer; transition: all 0.2s; }
      .tag-pill:hover { background: rgba(0, 188, 212, 0.3); border-color: #00e5ff; }
      .note-cell-wrapper { display: flex; flex-direction: column; width: 100%; min-height: 40px; justify-content: center; position: relative; z-index: 5; }
      .note-tags-container { display: flex; flex-wrap: wrap; gap: 4px; }
      .note-actions { display: flex; gap: 8px; margin-top: 6px; opacity: 0.6; transition: opacity 0.2s ease; z-index: 10; }
      .note-cell-wrapper:hover .note-actions { opacity: 1; }
      .btn-mini-action { border: none; padding: 2px 10px; border-radius: 12px; font-size: 0.75em; font-weight: bold; cursor: pointer; transition: all 0.2s; }
      .btn-edit-note { background: #333; color: var(--accent-primary); border: 1px solid var(--accent-primary); }
      .ghost-pill:hover { border-color: var(--accent-primary) !important; color: var(--accent-primary) !important; background: rgba(0, 188, 212, 0.1) !important; }
      .entry-text-clickable { font-weight: 500; font-size: 1.05em; color: #fff; margin-bottom: 8px; cursor: text; border-radius: 4px; padding: 2px; transition: background 0.2s; border: 1px solid transparent; }
      .entry-text-clickable:hover { background-color: rgba(255,255,255,0.05); border-color: #444; }
      
      details summary { color: var(--accent-primary); font-size: 0.8em; font-weight: 600; cursor: pointer; list-style: none; opacity: 0.8; transition: opacity 0.2s; }
      details summary:hover { opacity: 1; text-decoration: underline; }
      details summary::-webkit-details-marker { display: none; }
      details summary::before { content: '►'; display: inline-block; margin-right: 5px; font-size: 0.8em; transition: transform 0.2s; }
      details[open] summary::before { transform: rotate(90deg); }
      .entry-container { position: relative; }
      .entry-toolbar { margin-top: 8px; display: flex; gap: 10px; opacity: 0.4; transition: opacity 0.2s; align-items: center; }
      .entry-container:hover .entry-toolbar { opacity: 1; }
      .btn-entry-tool { background: transparent; border: 1px solid #444; color: #888; padding: 2px 8px; border-radius: 4px; font-size: 0.75em; cursor: pointer; }
      .btn-entry-tool:hover { border-color: #666; color: #ccc; }
      .btn-entry-tool-delete { border-color: #552222; color: #884444; }
      .btn-entry-tool-delete:hover { border-color: #ff5252; color: #ff5252; background: rgba(255,82,82,0.1); }
      .btn-history-active { border-color: #ffab40 !important; color: #ffab40 !important; }
      
      .attachment-grid { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }
      .attachment-thumb { display: block; border: 1px solid #444; border-radius: 4px; transition: transform 0.2s, border-color 0.2s; }
      .attachment-thumb:hover { transform: scale(1.05); border-color: var(--accent-primary); }
      
      .modal-content { background-color: #1E1E1E !important; border: 1px solid #444; color: #eee; }
      .modal-header { border-bottom: 1px solid #333; }
      .modal-footer { border-top: 1px solid #333; }
      .close { color: #fff; text-shadow: none; opacity: 0.8; }
      .close:hover { opacity: 1; color: var(--accent-primary); }
    ")),
    
    div(class = "hud-bar",
        # --- LEFT: TITLE ---
        h4("LAB NOTEBOOK", 
           style = "font-family: 'Montserrat', sans-serif; font-weight: 700; color: var(--accent-primary); margin: 0; letter-spacing: -1px; font-size: 1.6rem; margin-right: auto;"),
        
        # --- RIGHT: CLUSTER ---
        # Using align-items: center here ensures vertically aligned children (pills & grid)
        div(style = "display: flex; align-items: center; gap: 15px;",
            
            # FILTERS: MOSAIC CLUSTER
            uiOutput("category_breakdown", style = "width: auto;"),
            
            # DIVIDER
            div(style = "width: 1px; height: 40px; background: rgba(255, 255, 255, 0.15);"),
            
            # STATS
            div(style = "display: flex; gap: 10px;",
                div(class = "stat-pill", bsicons::bs_icon("journal-text"), textOutput("stat_total", inline = TRUE)),
                div(class = "stat-pill", bsicons::bs_icon("clock-history"), textOutput("stat_last", inline = TRUE))
            )
        )
    ),
    
    div(class = "notebook-card", style = "width: 100%;",
        DTOutput("table_notes") 
    ),
    
    div(id = "floating_editor",
        div(style = "flex-grow: 1;", selectizeInput("quick_note_input", label = NULL, choices = NULL, multiple = TRUE, options = list(create = TRUE, placeholder = "+ Tag", plugins = list('restore_on_backspace', 'remove_button')), width = "100%")),
        actionButton("save_quick_note", icon("check"))
    ),
    
    div(id = "floating_entry_editor",
        textAreaInput("entry_text_input", label = NULL, value = "", placeholder = "Edit entry text..."),
        div(class="editor-footer", 
            span(class="editor-hint", "Ctrl+Enter to save"),
            div(style="display: flex; gap: 5px;",
                actionButton("cancel_entry_inline", "Cancel", icon = icon("times"), class = "btn-secondary btn-sm"),
                actionButton("save_entry_inline", "Save", icon = icon("check"), class = "btn-primary btn-sm")
            )
        )
    )
  )
)

# --- SERVER ---
server <- function(input, output, session) {
  
  active_filter <- reactiveVal("All")
  observeEvent(input$filter_category, { active_filter(input$filter_category) })
  
  poll_data <- reactivePoll(4000, session,
                            checkFunc = function() { if (file.exists(DB_PATH)) { fi <- file.info(DB_PATH); paste(fi$mtime, fi$size) } else NULL },
                            valueFunc = function() {
                              con <- dbConnect(SQLite(), DB_PATH)
                              on.exit(dbDisconnect(con))
                              tryCatch({
                                query <- "
          SELECT l.id, l.timestamp, l.category, l.content, l.raw_transcript, l.manual_note,
                 (SELECT COUNT(*) FROM entry_history h WHERE h.parent_id = l.id) as version_count,
                 (SELECT GROUP_CONCAT(filename, '|') FROM entry_attachments a WHERE a.parent_id = l.id) as attach_files
          FROM lab_notes l
          WHERE l.deleted_at IS NULL
          ORDER BY l.timestamp DESC
        "
                                raw <- dbGetQuery(con, query) %>% as_tibble() %>% mutate(timestamp = as.POSIXct(timestamp, tz = "UTC"))
                                if(nrow(raw) == 0) return(tibble(id=integer(), timestamp=POSIXct(), display=character(), manual_note=character()))
                                
                                step1 <- raw %>% 
                                  mutate(
                                    raw_transcript = ifelse(is.na(raw_transcript) | raw_transcript == "", content, raw_transcript),
                                    category = stringr::str_to_title(category) 
                                  )
                                
                                cat_counts <- step1 %>% count(category) %>% arrange(desc(n))
                                
                                agg <- step1 %>%
                                  group_by(timestamp, raw_transcript) %>%
                                  summarise(
                                    id = first(id), 
                                    grouped_ids = paste(id, collapse = ","),
                                    categories_str = paste(unique(category), collapse = ","),
                                    insights_html = paste0("<div style='margin-top:4px; font-size:0.9em; color:#bbb; display:flex; align-items:flex-start;'><span class='badge' style='background:#333; border:1px solid #555; margin-right:8px; min-width:80px;'>", category, "</span><span>", content, "</span></div>", collapse=""),
                                    manual_note = paste(unique(na.omit(manual_note[manual_note != ""])), collapse="; "),
                                    version_count = max(version_count),
                                    attach_files = paste(unique(na.omit(attach_files)), collapse="|"), 
                                    .groups = "drop"
                                  )
                                
                                attr(agg, "cat_counts") <- cat_counts
                                
                                agg$attach_html <- map_chr(agg$attach_files, function(f_str) {
                                  if (is.na(f_str) || f_str == "") return("")
                                  files <- unique(unlist(strsplit(as.character(f_str), "\\|")))
                                  files <- files[files != ""]
                                  if(length(files) == 0) return("")
                                  imgs <- lapply(files, function(f) {
                                    path <- paste0("uploads/", f)
                                    paste0("<a href='", path, "' target='_blank'><img src='", path, "' class='attachment-thumb' height='60' width='60' style='object-fit: cover;' title='Click to enlarge'></a>")
                                  })
                                  paste0("<div class='attachment-grid'>", paste(imgs, collapse=""), "</div>")
                                })
                                
                                agg$display <- paste0(
                                  "<div class='entry-container'>",
                                  "<div class='entry-text-clickable' data-ids='", agg$grouped_ids, "' data-content='", gsub("'", "&#39;", agg$raw_transcript), "'>", agg$raw_transcript, "</div>",
                                  agg$attach_html,
                                  "<details style='margin-bottom: 5px; margin-top: 5px;'>",
                                  "<summary>Show Extraction</summary>",
                                  "<div style='margin-left: 5px; padding-left: 10px; border-left: 2px solid #444; margin-top: 5px;'>", agg$insights_html, "</div>",
                                  "</details>",
                                  "<div class='entry-toolbar'>",
                                  "<button class='btn-entry-tool' onclick='Shiny.setInputValue(\"action_add_attachment\", ", agg$id, ", {priority: \"event\"})'><i class='fa fa-paperclip'></i> Attach</button>",
                                  ifelse(agg$version_count > 0, paste0("<button class='btn-entry-tool btn-history-active' onclick='Shiny.setInputValue(\"action_view_history\", ", agg$id, ", {priority: \"event\"})'><i class='fa fa-clock-o'></i> Versions</button>"), ""),
                                  "<button class='btn-entry-tool btn-entry-tool-delete' onclick='Shiny.setInputValue(\"action_delete_entry\", \"", agg$grouped_ids, "\", {priority: \"event\"})' style='margin-left:auto;'><i class='fa fa-trash'></i></button>",
                                  "</div>",
                                  "</div>"
                                )
                                agg %>% arrange(desc(timestamp))
                              }, error = function(e) { print(e); tibble() })
                            }
  )
  
  output$stat_total <- renderText({ nrow(poll_data()) })
  output$stat_last <- renderText({ df <- poll_data(); if(nrow(df) > 0) paste0(round(difftime(Sys.time(), df$timestamp[1], units="hours"), 1), "h ago") else "N/A" })
  
  # --- CATEGORY PILLS (MOSAIC GRID WITH SMALLER ALL) ---
  output$category_breakdown <- renderUI({
    df <- poll_data()
    if(nrow(df) == 0) return(NULL)
    counts <- attr(df, "cat_counts")
    if(is.null(counts)) return(NULL)
    
    cat_colors <- list("Idea"="#ffab40", "Observation"="#00bcd4", "Todo"="#ff5252", "Protocol"="#00e676", "Data"="#7c4dff", "Maintenance"="#ff4081", "Discussion"="#2979ff", "General"="#888888")
    current_filter <- active_filter()
    
    # "All" Singleton - Smaller and Aligned
    all_active <- if(current_filter == "All") "active" else ""
    all_pill <- div(
      class = paste("filter-pill", all_active), 
      "data-cat" = "All", 
      style = "border: 1px solid #666; color: #eee; border-radius: 8px; padding: 6px 14px; font-size: 0.85em; font-weight: 700; margin-right: 15px; background: rgba(255,255,255,0.05); align-self: center; width: 15px;", 
      "All"
    )
    
    pills <- lapply(1:nrow(counts), function(i) {
      cat_name <- counts$category[i]
      count <- counts$n[i]
      color <- ifelse(!is.null(cat_colors[[cat_name]]), cat_colors[[cat_name]], "#888")
      is_active <- if(current_filter == cat_name) "active" else ""
      div(class = paste("filter-pill", is_active), "data-cat" = cat_name, style = paste0("border: 1px solid ", color, "; color: ", color, "; display: flex; align-items: center; gap: 5px; direction: ltr; background: rgba(0,0,0,0.2);"), span(style = paste0("background-color: ", color, "; width: 6px; height: 6px; border-radius: 50%; display: inline-block;")), paste(count, cat_name))
    })
    
    # PARENT CONTAINER: display:flex; align-items:center handles vertical centering of 'All' pill
    tagList(all_pill, div(style = "display: flex; flex-direction: column; flex-wrap: wrap; height: 85px; gap: 3px; direction: rtl; align-content: flex-start;", pills))
  })
  
  output$table_notes <- renderDT({
    df <- poll_data()
    if(nrow(df) == 0) return(NULL)
    filter_val <- active_filter()
    if (filter_val != "All") { df <- df %>% filter(grepl(filter_val, categories_str)) }
    df$note_render <- map_chr(1:nrow(df), function(i) {
      row <- df[i,]
      if (is.na(row$manual_note) || row$manual_note == "") {
        paste0("<span class='ghost-pill' onclick='Shiny.setInputValue(\"note_action_new\", {id: \"ghost_new\", row: 0, db_id: ", row$id, "}, {priority: \"event\"})' style='border: 1px dashed #555; color: #666; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; cursor: pointer; display: inline-block;'>+ Tag</span>")
      } else {
        tags_vec <- strsplit(row$manual_note, ";")[[1]]
        tags_vec <- trimws(tags_vec)
        tags_html <- paste0("<span class='tag-pill' onclick='Shiny.setInputValue(\"note_action_edit\", ", row$id, ", {priority: \"event\"})'>", tags_vec, "</span>", collapse = "")
        paste0("<div class='note-cell-wrapper'><div class='note-tags-container'>", tags_html, "</div><div class='note-actions'><button class='btn-mini-action btn-edit-note' onclick='Shiny.setInputValue(\"note_action_edit\", ", row$id, ", {priority: \"event\"})'>Edit</button><button class='btn-mini-action btn-append-note' onclick='Shiny.setInputValue(\"note_action_append\", ", row$id, ", {priority: \"event\"})'>+</button></div></div>")
      }
    })
    
    # --- DOM: 'ft' RESTORES SEARCH BAR ---
    datatable(df %>% select(ID = id, Date = timestamp, Entry = display, `My Notes` = note_render), escape = FALSE, selection = "single", rownames = FALSE, editable = FALSE, 
              options = list(pageLength = 200, dom = 'ft', autoWidth = FALSE, columnDefs = list(list(visible = FALSE, targets = 0), list(width = '130px', targets = 1), list(width = '35%', targets = 3)), language = list(search = "Search:")), 
              callback = JS("// No blocking callback")) %>% formatDate(2, method = "toLocaleString")
  })
  
  observeEvent(input$action_add_attachment, { session$userData$attach_parent_id <- input$action_add_attachment; showModal(modalDialog(title = "Attach Image", fileInput("upload_file", "Choose Image/PDF", accept = c("image/*", ".pdf")), footer = modalButton("Close"), size = "m", easyClose = TRUE)) })
  observeEvent(input$upload_file, { req(input$upload_file, session$userData$attach_parent_id); ext <- tools::file_ext(input$upload_file$name); new_filename <- paste0("img_", format(Sys.time(), "%Y%m%d_%H%M%S_"), UUIDgenerate(TRUE), ".", ext); target_path <- file.path(UPLOAD_DIR, new_filename); file.copy(input$upload_file$datapath, target_path); con <- dbConnect(SQLite(), DB_PATH); dbExecute(con, "INSERT INTO entry_attachments (parent_id, filename) VALUES (?, ?)", list(session$userData$attach_parent_id, new_filename)); dbDisconnect(con); removeModal(); showNotification("Attachment Added", type = "message") })
  observeEvent(input$trigger_inline_edit, { data <- input$trigger_inline_edit; session$userData$editing_entry_ids <- data$ids; updateTextAreaInput(session, "entry_text_input", value = data$content) })
  
  observeEvent(input$save_entry_inline, { req(session$userData$editing_entry_ids); new_text <- trimws(input$entry_text_input); ids_str <- as.character(session$userData$editing_entry_ids); ids_vec <- strsplit(ids_str, ",")[[1]]; anchor_id <- as.integer(ids_vec[1]); ids_sql <- paste(as.integer(ids_vec), collapse = ","); con <- dbConnect(SQLite(), DB_PATH); tryCatch({ sig_row <- dbGetQuery(con, paste0("SELECT raw_transcript FROM lab_notes WHERE id = ", anchor_id)); old_text <- if(nrow(sig_row) > 0) sig_row$raw_transcript[1] else ""; if(new_text == old_text) { shinyjs::runjs("$('#floating_entry_editor').hide();"); showNotification("No changes made", type = "warning", duration = 2) } else { dbBegin(con); if(nrow(sig_row) > 0) { dbExecute(con, "INSERT INTO entry_history (parent_id, old_transcript) VALUES (?, ?)", list(anchor_id, old_text)); dbExecute(con, sprintf("UPDATE lab_notes SET raw_transcript = ? WHERE id IN (%s)", ids_sql), list(new_text)); dbCommit(con); showNotification("Entry Updated", type = "message", duration = 1); shinyjs::runjs("$('#floating_entry_editor').hide();") } } }, error = function(e) { dbRollback(con); print(e) }, finally = { dbDisconnect(con) }) })
  observeEvent(input$action_delete_entry, { session$userData$delete_ids <- input$action_delete_entry; showModal(modalDialog(title = "Confirm Deletion", "Are you sure you want to delete this entry? This acts as a soft delete.", footer = tagList(modalButton("Cancel"), actionButton("confirm_delete", "Delete", class = "btn-danger")), size = "s")) })
  observeEvent(input$confirm_delete, { req(session$userData$delete_ids); ids_str <- as.character(session$userData$delete_ids); ids_sql <- paste(as.integer(strsplit(ids_str, ",")[[1]]), collapse = ","); con <- dbConnect(SQLite(), DB_PATH); tryCatch({ dbExecute(con, sprintf("UPDATE lab_notes SET deleted_at = CURRENT_TIMESTAMP WHERE id IN (%s)", ids_sql)); showNotification("Entry Deleted", type = "warning") }, finally = { dbDisconnect(con) }); removeModal() })
  observeEvent(input$action_view_history, { target_id <- input$action_view_history; con <- dbConnect(SQLite(), DB_PATH); history_df <- dbGetQuery(con, paste0("SELECT archived_at, old_transcript FROM entry_history WHERE parent_id = ", target_id, " ORDER BY archived_at DESC")); dbDisconnect(con); if(nrow(history_df) > 0) { history_html <- lapply(1:nrow(history_df), function(i) { div(style = "border-bottom: 1px solid #444; padding: 10px 0;", div(style = "color: #ffab40; font-size: 0.85em; font-weight: bold; margin-bottom: 5px;", icon("clock-o"), history_df$archived_at[i]), div(style = "color: #ccc; white-space: pre-wrap; font-family: monospace; background: #111; padding: 10px; border-radius: 5px;", history_df$old_transcript[i])) }); showModal(modalDialog(title = "Version History", div(style = "max-height: 60vh; overflow-y: auto;", history_html), size = "l", easyClose = TRUE, footer = modalButton("Close"))) } })
  get_all_tags <- function(df) { all_notes <- df$manual_note[!is.na(df$manual_note) & df$manual_note != ""]; unique(trimws(unlist(strsplit(all_notes, ";")))) }
  observeEvent(input$note_action_new, { target_id <- input$note_action_new$db_id; df <- poll_data(); session$userData$edit_id <- target_id; history <- get_all_tags(df); updateSelectizeInput(session, "quick_note_input", choices = history, selected = NULL, server = TRUE); runjs("$('#quick_note_input').selectize()[0].selectize.focus();") })
  observeEvent(input$note_action_edit, { target_id <- input$note_action_edit; df <- poll_data(); target_row <- df %>% filter(id == target_id) %>% slice(1); session$userData$edit_id <- target_id; history <- get_all_tags(df); current_tags <- trimws(strsplit(target_row$manual_note, ";")[[1]]); updateSelectizeInput(session, "quick_note_input", choices = history, selected = current_tags, server = TRUE); runjs("$('#quick_note_input').selectize()[0].selectize.focus();") })
  observeEvent(input$note_action_append, { target_id <- input$note_action_append; df <- poll_data(); target_row <- df %>% filter(id == target_id) %>% slice(1); session$userData$edit_id <- target_id; history <- get_all_tags(df); current_tags <- trimws(strsplit(target_row$manual_note, ";")[[1]]); updateSelectizeInput(session, "quick_note_input", choices = history, selected = current_tags, server = TRUE); runjs("$('#quick_note_input').selectize()[0].selectize.focus();") })
  observeEvent(input$save_quick_note, { req(session$userData$edit_id); new_tags_vec <- input$quick_note_input; new_text <- paste(new_tags_vec, collapse = ";"); con <- dbConnect(SQLite(), DB_PATH); dbExecute(con, "UPDATE lab_notes SET manual_note = ? WHERE id = ?", list(new_text, session$userData$edit_id)); dbDisconnect(con); shinyjs::hide("floating_editor"); showNotification("Tags Updated", type = "message", duration = 1) })
}

shinyApp(ui, server)