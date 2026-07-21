library(DBI)
library(RSQLite)
library(tidyverse)

# 1. Connect to the Vault
con <- dbConnect(RSQLite::SQLite(), "C:\\Users\\chase\\Documents\\Everything_Python\\lab_notebook.db")

# 2. Pull Data (Updated for Schema v2)
# We changed 's.sample_label' to 's.sample_id'
df <- dbGetQuery(con, "
  SELECT e.experiment_name, s.sample_id, s.titer_value, s.status
  FROM samples s
  JOIN experiments e ON s.experiment_id = e.id
") %>%
  as_tibble()

# 3. Validation (Strict Assertions)
stopifnot(is.numeric(df$titer_value))
if(any(df$titer_value < 0)) warning("Negative titer detected! Investigation required.")

# 4. Visualize
print(df)

# Cleanup
dbDisconnect(con)
